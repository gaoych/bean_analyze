"""Simple web application to visualize bean dependency chains."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from functools import partial
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

        metadata_map[name] = {
            "name": name,
            "type": bean.get("type", ""),
            "scope": bean.get("scope", ""),
            "categories": bean.get("categories", []),
            "source": bean.get("source", ""),
            "definitionSource": bean.get("definitionSource", ""),
            "isAdditionalBean": bean.get("isAdditionalBean", False),
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
                    "missing": True,
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
    }


GRAPH = load_graph()


def build_subgraph(root: Optional[str]) -> Dict[str, object]:
    """Return the graph filtered to nodes reachable from the given root."""
    if not root or root.lower() == "all":
        nodes = list(GRAPH["nodes"].values())
        leaf_count = sum(1 for node in nodes if not node["hasDependencies"])
        return {
            "nodes": nodes,
            "edges": GRAPH["edges"],
            "roots": GRAPH["roots"],
            "selectedRoot": None,
            "chainSummary": {
                "root": None,
                "nodeCount": len(nodes),
                "leafCount": leaf_count,
                "unusedRootCount": len(GRAPH["unused_roots_list"]),
            },
        }

    if root not in GRAPH["nodes"]:
        raise KeyError(root)

    visited: Set[str] = set()
    queue: deque[str] = deque([root])

    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for dependency in GRAPH["dependencies"].get(current, []):
            if dependency not in visited:
                queue.append(dependency)

    nodes = [GRAPH["nodes"][name] for name in visited]
    edges = [edge for edge in GRAPH["edges"] if edge["source"] in visited and edge["target"] in visited]

    external_referencers: Set[str] = set()
    externally_referenced_nodes = 0
    for node_name in visited:
        dependents = GRAPH["nodes"][node_name]["dependents"]
        outside_dependents = [dependent for dependent in dependents if dependent not in visited]
        if outside_dependents:
            externally_referenced_nodes += 1
            external_referencers.update(outside_dependents)

    leaf_count = GRAPH["chain_leaf_counts"].get(root, 0)
    chain_summary = {
        "root": root,
        "nodeCount": len(nodes),
        "leafCount": leaf_count,
        "isUnused": root in GRAPH["unused_roots_lookup"],
        "externallyReferencedNodes": externally_referenced_nodes,
        "externalReferencerCount": len(external_referencers),
    }

    return {
        "nodes": nodes,
        "edges": edges,
        "roots": GRAPH["roots"],
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
            try:
                payload = build_subgraph(root)
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
            payload = {
                "roots": GRAPH["roots"],
                "unusedChains": GRAPH["unused_roots_list"],
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
