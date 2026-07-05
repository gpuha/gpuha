"""Minimal YAML-subset loader + topology schema. Stdlib-only (no PyYAML dep).

Supports exactly the constrained subset the orchestrator needs:
  - nested mappings (key: value / key: then indented block)
  - lists of scalars (- value) and lists of mappings (- key: value ...)
  - scalars typed as int/float/bool/null, else string (optional quotes)
  - '#' comments and blank lines
Indentation is significant; use 2 spaces per level (tabs rejected).
Also accepts JSON transparently (if the file content starts with '{' or '[').
"""
import json, os
from dataclasses import dataclass, field


class TopologyError(Exception):
    pass


def _scalar(tok: str):
    t = tok.strip()
    if t == "" or t == "~" or t.lower() == "null":
        return None
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1]
    low = t.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _indent(line: str) -> int:
    if "\t" in (line[: len(line) - len(line.lstrip())]):
        raise TopologyError("tabs not allowed in indentation: %r" % line)
    return len(line) - len(line.lstrip(" "))


def _clean(raw: str):
    out = []
    for line in raw.splitlines():
        if "#" in line:
            line = line.split("#", 1)[0]
        if line.strip() == "":
            continue
        out.append((_indent(line), line.strip()))
    return out


def _parse_block(lines, i, indent):
    if lines[i][1].startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines, i, indent):
    d = {}
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise TopologyError("unexpected indent at: %r" % text)
        if ":" not in text:
            raise TopologyError("expected 'key: value' at: %r" % text)
        key, _, rest = text.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                val, i = _parse_block(lines, i + 1, lines[i + 1][0])
                d[key] = val
                continue
            d[key] = None
            i += 1
        else:
            d[key] = _scalar(rest)
            i += 1
    return d, i


def _parse_list(lines, i, indent):
    items = []
    while i < len(lines):
        ind, text = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise TopologyError("unexpected indent in list at: %r" % text)
        if not text.startswith("- "):
            break
        body = text[2:].strip()
        if ":" in body:
            key, _, rest = body.partition(":")
            item = {}
            if rest.strip() == "":
                if i + 1 < len(lines) and lines[i + 1][0] > ind:
                    val, i = _parse_block(lines, i + 1, lines[i + 1][0])
                    item[key.strip()] = val
                else:
                    item[key.strip()] = None
                    i += 1
            else:
                item[key.strip()] = _scalar(rest)
                i += 1
            child_indent = ind + 2
            while i < len(lines) and lines[i][0] == child_indent and not lines[i][1].startswith("- "):
                k2, _, r2 = lines[i][1].partition(":")
                if r2.strip() == "" and i + 1 < len(lines) and lines[i + 1][0] > child_indent:
                    v2, i = _parse_block(lines, i + 1, lines[i + 1][0])
                    item[k2.strip()] = v2
                else:
                    item[k2.strip()] = _scalar(r2)
                    i += 1
            items.append(item)
        else:
            items.append(_scalar(body))
            i += 1
    return items, i


def parse_yaml_subset(raw: str):
    s = raw.lstrip()
    if s.startswith("{") or s.startswith("["):
        return json.loads(raw)
    lines = _clean(raw)
    if not lines:
        return {}
    val, _ = _parse_block(lines, 0, lines[0][0])
    return val


@dataclass
class PoolSpec:
    name: str
    provider: str
    role: str = "stub"
    region: str = ""
    machine_type: str = ""
    workers: int = 1
    warmup_budget: int = 0
    backend: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.backend:
            self.backend = self.name


@dataclass
class Topology:
    name: str
    min_pools: int
    plane: dict
    pools: list

    @property
    def plane_ip(self):
        return self.plane.get("host", "")

    @property
    def telem_port(self):
        return int(self.plane.get("tier1_telem_port", 5106))

    @property
    def pool_map_file(self):
        return self.plane.get("pool_map_file", "~/gpuha-runs/tier1-pools.map")


COLD_WARMUP_DEFAULT = 1800  # cold-GPU verify budget (s) when a pool sets no warmup_budget
_KNOWN_POOL_KEYS = {"name", "provider", "role", "region", "machine_type", "workers", "backend", "warmup_budget"}


def load_topology(path) -> Topology:
    with open(os.path.expanduser(path)) as f:
        raw = f.read()
    d = parse_yaml_subset(raw)
    if not isinstance(d, dict):
        raise TopologyError("topology must be a mapping")
    if "name" not in d:
        raise TopologyError("topology missing 'name'")
    if "pools" not in d or not isinstance(d["pools"], list) or not d["pools"]:
        raise TopologyError("topology needs a non-empty 'pools' list")
    pools = []
    for p in d["pools"]:
        if not isinstance(p, dict) or "name" not in p or "provider" not in p:
            raise TopologyError("each pool needs at least name+provider: %r" % (p,))
        extra = {k: v for k, v in p.items() if k not in _KNOWN_POOL_KEYS}
        pools.append(PoolSpec(
            name=p["name"], provider=p["provider"],
            role=p.get("role", "stub"), region=p.get("region", ""),
            machine_type=p.get("machine_type", ""),
            workers=int(p.get("workers", 1)),
            warmup_budget=int(p.get("warmup_budget", 0)),
            backend=p.get("backend", ""), extra=extra))
    min_pools = int(d.get("min_pools", len(pools)))
    plane = d.get("plane", {}) or {}
    return Topology(name=d["name"], min_pools=min_pools, plane=plane, pools=pools)
