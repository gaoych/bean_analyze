"""Simple web application to visualize bean dependency chains."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from functools import lru_cache, partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "iuap-apdoc-basedoc.json"


def _normalize_dependency_list(raw: Optional[Iterable[str]]) -> List[str]:
    """Return a clean list of dependency names without empty values."""
    if not raw:
        return []
    return [item for item in raw if item]


def load_graph() -> Dict[str, object]:
    """Build graph information from the bean description file."""
    if not DATA_FILE.exists():
        raise FileNotFoundError(
            f"Cannot find {DATA_FILE}. Please make sure the bean description JSON is available."
        )

    with DATA_FILE.open("r", encoding="utf-8") as handle:
        bean_definitions = json.load(handle)

    dependencies_map: Dict[str, List[str]] = {}
    metadata_map: Dict[str, Dict[str, object]] = {}

    for bean in bean_definitions:
        name = bean.get("name")
        if not name:
            # Skip entries without a valid bean name.
            continue
        dependencies = _normalize_dependency_list(bean.get("dependencies"))
        dependencies_map[name] = dependencies

        source_value = bean.get("source", "")
        is_spring = bool(
            (source_value and source_value.lower().startswith("spring"))
            or (name and name.startswith("org.springframework"))
        )

        metadata_map[name] = {
            "name": name,
            "type": bean.get("type", ""),
            "scope": bean.get("scope", ""),
            "categories": bean.get("categories", []),
            "source": bean.get("source", ""),
            "definitionSource": bean.get("definitionSource", ""),
            "isAdditionalBean": bean.get("isAdditionalBean", False),
            "additionalBeanSource": bean.get("additionalBeanSource", ""),
            "isSpringBean": is_spring,
        }

    # Add placeholder nodes for dependencies that do not have their own definitions.
    for bean_name, dependencies in list(dependencies_map.items()):
        for dependency in dependencies:
            dependencies_map.setdefault(dependency, [])
            if dependency not in metadata_map:
                metadata_map[dependency] = {
                    "name": dependency,
                    "type": "External or undefined bean",
                    "scope": "unknown",
                    "categories": [],
                    "source": "Unknown",
                    "definitionSource": "",
                    "isAdditionalBean": False,
                    "additionalBeanSource": "",
                    "missing": True,
                    "isSpringBean": False,
                }

    incoming_map: Dict[str, Set[str]] = defaultdict(set)
    for bean_name, dependencies in dependencies_map.items():
        for dependency in dependencies:
            incoming_map[dependency].add(bean_name)
        incoming_map.setdefault(bean_name, set())

    nodes: Dict[str, Dict[str, object]] = {}
    edges: List[Dict[str, str]] = []

    for bean_name, dependencies in dependencies_map.items():
        metadata = metadata_map.get(bean_name, {"name": bean_name})
        node = {
            "id": bean_name,
            "label": bean_name,
            "dependencies": list(dependencies),
            "dependents": sorted(incoming_map.get(bean_name, set())),
            "hasDependencies": bool(dependencies),
            "dependentCount": len(incoming_map.get(bean_name, set())),
            "isRoot": len(incoming_map.get(bean_name, set())) == 0,
            "missing": metadata.get("missing", False),
            "metadata": metadata,
            "isSpringBean": metadata.get("isSpringBean", False),
        }
        nodes[bean_name] = node

        for dependency in dependencies:
            edges.append({"source": bean_name, "target": dependency})

    roots = sorted(name for name, node in nodes.items() if node["isRoot"])

    chain_leaf_counts: Dict[str, int] = {}
    chain_nodes_map: Dict[str, Set[str]] = {}
    unused_roots: List[Dict[str, object]] = []
    unused_roots_lookup: Dict[str, Dict[str, object]] = {}

    for root in roots:
        visited: Set[str] = set()
        queue: deque[str] = deque([root])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for dependency in dependencies_map.get(current, []):
                if dependency not in visited:
                    queue.append(dependency)

        chain_nodes_map[root] = visited
        leaf_count = sum(1 for node in visited if not dependencies_map.get(node))
        chain_leaf_counts[root] = leaf_count

        has_external_usage = False
        for node_name in visited:
            dependents = incoming_map.get(node_name, set())
            if any(dependent not in visited for dependent in dependents):
                has_external_usage = True
                break

        if not has_external_usage:
            info = {"root": root, "nodeCount": len(visited), "leafCount": leaf_count}
            unused_roots.append(info)
            unused_roots_lookup[root] = info

    unused_roots.sort(key=lambda item: (-item["nodeCount"], item["root"]))

    spring_nodes = {name for name, node in nodes.items() if node.get("isSpringBean")}

    return {
        "nodes": nodes,
        "edges": edges,
        "roots": roots,
        "dependencies": dependencies_map,
        "incoming": incoming_map,
        "chain_nodes": chain_nodes_map,
        "chain_leaf_counts": chain_leaf_counts,
        "unused_roots_list": unused_roots,
        "unused_roots_lookup": unused_roots_lookup,
        "spring_nodes": spring_nodes,
    }


GRAPH = load_graph()

def _parse_bool(value: Optional[str]) -> bool:
    """Return True if the string represents a truthy value."""

    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def build_filtered_graph(exclude_spring: bool) -> Dict[str, object]:
    """Return a graph view with optional filters applied."""

    if not exclude_spring:
        return GRAPH

    excluded_nodes = GRAPH.get("spring_nodes", set())
    if not excluded_nodes:
        return GRAPH

    dependencies: Dict[str, List[str]] = {}
    incoming: Dict[str, Set[str]] = defaultdict(set)

    for bean_name, deps in GRAPH["dependencies"].items():
        if bean_name in excluded_nodes:
            continue

        filtered_deps = [dep for dep in deps if dep not in excluded_nodes]
        dependencies[bean_name] = filtered_deps

        for dependency in filtered_deps:
            incoming[dependency].add(bean_name)
        incoming.setdefault(bean_name, set())

    nodes: Dict[str, Dict[str, object]] = {}
    edges: List[Dict[str, str]] = []

    for bean_name, deps in dependencies.items():
        base = GRAPH["nodes"][bean_name]
        dependents = sorted(incoming.get(bean_name, set()))
        node = {
            "id": bean_name,
            "label": base["label"],
            "dependencies": list(deps),
            "dependents": dependents,
            "hasDependencies": bool(deps),
            "dependentCount": len(dependents),
            "isRoot": len(dependents) == 0,
            "missing": base.get("missing", False),
            "metadata": base.get("metadata", {}),
            "isSpringBean": base.get("isSpringBean", False),
        }
        nodes[bean_name] = node

        for dependency in deps:
            edges.append({"source": bean_name, "target": dependency})

    roots = sorted(name for name, node in nodes.items() if node["isRoot"])

    chain_nodes_map: Dict[str, Set[str]] = {}
    chain_leaf_counts: Dict[str, int] = {}
    unused_roots: List[Dict[str, object]] = []
    unused_lookup: Dict[str, Dict[str, object]] = {}

    for root in roots:
        visited: Set[str] = set()
        queue: deque[str] = deque([root])

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for dependency in dependencies.get(current, []):
                if dependency not in visited:
                    queue.append(dependency)

        chain_nodes_map[root] = visited
        leaf_count = sum(1 for node in visited if not dependencies.get(node))
        chain_leaf_counts[root] = leaf_count

        has_external_usage = False
        for node_name in visited:
            dependents = incoming.get(node_name, set())
            if any(dependent not in visited for dependent in dependents):
                has_external_usage = True
                break

        if not has_external_usage:
            info = {"root": root, "nodeCount": len(visited), "leafCount": leaf_count}
            unused_roots.append(info)
            unused_lookup[root] = info

    unused_roots.sort(key=lambda item: (-item["nodeCount"], item["root"]))

    return {
        "nodes": nodes,
        "edges": edges,
        "roots": roots,
        "dependencies": dependencies,
        "incoming": incoming,
        "chain_nodes": chain_nodes_map,
        "chain_leaf_counts": chain_leaf_counts,
        "unused_roots_list": unused_roots,
        "unused_roots_lookup": unused_lookup,
        "spring_nodes": set(),
    }


@lru_cache(maxsize=2)
def get_graph(exclude_spring: bool) -> Dict[str, object]:
    """Return cached graph data with optional filters applied."""

    return build_filtered_graph(exclude_spring)


def build_subgraph(root: Optional[str], *, exclude_spring: bool = False) -> Dict[str, object]:
    """Return the graph filtered to nodes reachable from the given root."""
    graph = get_graph(exclude_spring)

    if not root or root.lower() == "all":
        nodes = list(graph["nodes"].values())
        leaf_count = sum(1 for node in nodes if not node["hasDependencies"])
        return {
            "nodes": nodes,
            "edges": graph["edges"],
            "roots": graph["roots"],
            "selectedRoot": None,
            "chainSummary": {
                "root": None,
                "nodeCount": len(nodes),
                "leafCount": leaf_count,
                "unusedRootCount": len(graph["unused_roots_list"]),
            },
        }

    if root not in graph["nodes"]:
        raise KeyError(root)

    visited: Set[str] = set()
    queue: deque[str] = deque([root])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for dependency in graph["dependencies"].get(current, []):
            if dependency not in visited:
                queue.append(dependency)

    nodes = [graph["nodes"][name] for name in visited]
    edges = [edge for edge in graph["edges"] if edge["source"] in visited and edge["target"] in visited]

    external_referencers: Set[str] = set()
    externally_referenced_nodes = 0
    for node_name in visited:
        dependents = graph["nodes"][node_name]["dependents"]
        outside_dependents = [dependent for dependent in dependents if dependent not in visited]
        if outside_dependents:
            externally_referenced_nodes += 1
            external_referencers.update(outside_dependents)

    leaf_count = graph["chain_leaf_counts"].get(root, 0)
    chain_summary = {
        "root": root,
        "nodeCount": len(nodes),
        "leafCount": leaf_count,
        "isUnused": root in graph["unused_roots_lookup"],
        "externallyReferencedNodes": externally_referenced_nodes,
        "externalReferencerCount": len(external_referencers),
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "roots": graph["roots"],
        "selectedRoot": root,
        "isUnusedChain": chain_summary["isUnused"],
        "chainSummary": chain_summary,
    }


class GraphRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler serving the visualization assets and data."""

    def do_GET(self) -> None:  # noqa: N802 (method name is required by base class)
        parsed_url = urlparse(self.path)
        if parsed_url.path == "/graph-data":
            params = parse_qs(parsed_url.query)
            root = params.get("root", [None])[0]
            exclude_spring = _parse_bool((params.get("excludeSpring") or [None])[0])
            try:
                payload = build_subgraph(root, exclude_spring=exclude_spring)
            except KeyError:
                self.send_error(HTTPStatus.NOT_FOUND, f"Unknown bean '{root}'")
                return

            response = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if parsed_url.path == "/roots":
            params = parse_qs(parsed_url.query)
            exclude_spring = _parse_bool((params.get("excludeSpring") or [None])[0])
            graph = get_graph(exclude_spring)
            payload = {
                "roots": graph["roots"],
                "unusedChains": graph["unused_roots_list"],
            }
            response = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        if parsed_url.path == "/":
            self.path = "/static/index.html"

        return super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003 (shadow builtins)
        """Silence default logging to keep the console clean."""
        # Comment out the next line to enable default logging.
        return


def serve(port: int) -> None:
    """Start the HTTP server."""
    handler = partial(GraphRequestHandler, directory=str(BASE_DIR))
    with ThreadingTCPServer(("0.0.0.0", port), handler) as server:
        print(f"Bean dependency visualizer available at http://localhost:{port}/")
        print("Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping server...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Visualize bean dependency chains from a JSON file.")
    parser.add_argument("--port", type=int, default=8000, help="Port to expose the web interface on.")
    args = parser.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
