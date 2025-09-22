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
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "iuap-apdoc-basedoc.json"


def _normalize_dependency_list(raw: Optional[Iterable[str]]) -> List[str]:
    """Return a clean list of dependency names without empty values."""
    if not raw:
        return []
    return [item for item in raw if item]


def _infer_third_party_package(bean: Dict[str, object]) -> Optional[str]:
    """Infer a displayable package identifier for third-party beans."""

    candidate = bean.get("type") or bean.get("name") or ""
    if not isinstance(candidate, str):
        return None

    parts = [part for part in candidate.split(".") if part]
    if len(parts) >= 3:
        return ".".join(parts[:3])
    if len(parts) >= 2:
        return ".".join(parts[:2])
    if parts:
        return parts[0]
    return None


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
        is_third_party = isinstance(source_value, str) and source_value == "Third Party Library"
        third_party_package = _infer_third_party_package(bean) if is_third_party else None

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
            "isThirdPartyBean": is_third_party,
            "thirdPartyPackage": third_party_package,
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
                    "isThirdPartyBean": False,
                    "thirdPartyPackage": None,
                }

    third_party_nodes: Set[str] = set()
    third_party_package_to_nodes: Dict[str, Set[str]] = defaultdict(set)
    node_to_third_party_package: Dict[str, Optional[str]] = {}

    for bean_name, metadata in metadata_map.items():
        package_name = metadata.get("thirdPartyPackage")
        node_to_third_party_package[bean_name] = package_name
        if metadata.get("isThirdPartyBean"):
            third_party_nodes.add(bean_name)
            if package_name:
                third_party_package_to_nodes[package_name].add(bean_name)

    third_party_packages_list = [
        {"package": package, "beanCount": len(nodes)}
        for package, nodes in third_party_package_to_nodes.items()
    ]
    third_party_packages_list.sort(key=lambda item: (-item["beanCount"], item["package"]))

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
            "isThirdPartyBean": metadata.get("isThirdPartyBean", False),
            "thirdPartyPackage": metadata.get("thirdPartyPackage"),
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
        "third_party_nodes": third_party_nodes,
        "third_party_packages": third_party_packages_list,
        "third_party_package_nodes": {
            package: frozenset(node_names)
            for package, node_names in third_party_package_to_nodes.items()
        },
        "node_third_party_package": node_to_third_party_package,
    }


GRAPH = load_graph()


def _parse_bool(value: Optional[str]) -> bool:
    """Return True if the string represents a truthy value."""

    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_list(values: Optional[List[str]]) -> List[str]:
    """Parse a comma-separated list from repeated query parameters."""

    if not values:
        return []

    parsed: List[str] = []
    for value in values:
        if value is None:
            continue
        for part in value.split(","):
            item = part.strip()
            if item:
                parsed.append(item)
    return parsed


def build_filtered_graph(
    exclude_spring: bool,
    *,
    exclude_third_party: bool = False,
    third_party_packages: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    """Return a graph view with optional filters applied."""

    if not exclude_spring and not exclude_third_party:
        return GRAPH

    excluded_nodes: Set[str] = set()

    if exclude_spring:
        excluded_nodes.update(GRAPH.get("spring_nodes", set()))

    if exclude_third_party:
        requested_packages = set(third_party_packages or [])
        if requested_packages:
            for package in requested_packages:
                excluded_nodes.update(
                    GRAPH["third_party_package_nodes"].get(package, set())
                )
        else:
            excluded_nodes.update(GRAPH.get("third_party_nodes", set()))

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
            "isThirdPartyBean": base.get("isThirdPartyBean", False),
            "thirdPartyPackage": base.get("thirdPartyPackage"),
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
        "third_party_nodes": set(),
        "third_party_packages": GRAPH["third_party_packages"],
        "third_party_package_nodes": GRAPH["third_party_package_nodes"],
        "node_third_party_package": GRAPH["node_third_party_package"],
    }


@lru_cache(maxsize=32)
def get_graph(
    exclude_spring: bool,
    exclude_third_party: bool,
    third_party_packages: Tuple[str, ...],
) -> Dict[str, object]:
    """Return cached graph data with optional filters applied."""

    return build_filtered_graph(
        exclude_spring,
        exclude_third_party=exclude_third_party,
        third_party_packages=third_party_packages,
    )


def build_subgraph(
    root: Optional[str],
    *,
    exclude_spring: bool = False,
    exclude_third_party: bool = False,
    third_party_packages: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    """Return the graph filtered to nodes reachable from the given root."""

    third_party_tuple: Tuple[str, ...] = tuple(
        sorted(third_party_packages) if third_party_packages else ()
    )
    graph = get_graph(exclude_spring, exclude_third_party, third_party_tuple)

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
            "thirdPartyPackages": graph["third_party_packages"],
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
        "thirdPartyPackages": graph["third_party_packages"],
    }


class GraphRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler serving the visualization assets and data."""

    def do_GET(self) -> None:  # noqa: N802 (method name is required by base class)
        parsed_url = urlparse(self.path)
        if parsed_url.path == "/graph-data":
            params = parse_qs(parsed_url.query)
            root = params.get("root", [None])[0]
            exclude_spring = _parse_bool((params.get("excludeSpring") or [None])[0])
            exclude_third_party = _parse_bool((params.get("excludeThirdParty") or [None])[0])
            third_party_packages = _parse_list(params.get("thirdPartyPackages"))
            try:
                payload = build_subgraph(
                    root,
                    exclude_spring=exclude_spring,
                    exclude_third_party=exclude_third_party,
                    third_party_packages=third_party_packages,
                )
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
            exclude_third_party = _parse_bool((params.get("excludeThirdParty") or [None])[0])
            third_party_packages = tuple(
                sorted(_parse_list(params.get("thirdPartyPackages")))
            )
            graph = get_graph(exclude_spring, exclude_third_party, third_party_packages)
            payload = {
                "roots": graph["roots"],
                "unusedChains": graph["unused_roots_list"],
                "thirdPartyPackages": graph["third_party_packages"],
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
