"""Read local Git worktrees and index declared Cyrene integration surfaces.

Usage: python -X utf8 inventory_interfaces.py --repo Client=C:/checkout/client
       --repo Plugins=C:/checkout/plugins --output snapshot.json
Requires PyYAML for OpenAPI/capability catalogs. Does not import project modules,
fetch remotes, run servers, or modify input repositories.
"""
from __future__ import annotations
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import yaml

EXCLUDE = {"node_modules", "target", ".venv", "dist", "tests", "test", "__tests__", "fixtures", "examples", "test-results"}
INVENTORY_STATUS_FILES = {
    ".agents/skills/cyrene-plugin-development/references/interface-inventory.json",
    ".agents/skills/cyrene-plugin-development/references/system-interfaces.md",
}
VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Exclude the inventory's two generated artifacts from status to avoid self-counting.
def worktree_change_count(root):
    changes = git(root, "status", "--porcelain").splitlines()
    return sum(change[3:] not in INVENTORY_STATUS_FILES for change in changes)

def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], encoding="utf-8", errors="strict").strip()

def literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None

def route_literal(node, constants):
    if isinstance(node, ast.Name): return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = route_literal(node.left, constants), route_literal(node.right, constants)
        return left + right if isinstance(left, str) and isinstance(right, str) else None
    return literal(node)

def scan(name, root):
    root = root.resolve(strict=True)
    files = sorted(set(git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")) - {""})
    entries, errors, sources = [], [], {}
    def add(kind, source, **values):
        entries.append({"kind": kind, "source": source, **values})
    for relative in files:
        path = root / relative
        if set(Path(relative).parts) & EXCLUDE or not path.is_file() or path.is_symlink():
            continue
        if root not in path.resolve().parents:
            continue
        suffix = path.suffix
        if suffix not in {".py", ".ts", ".tsx", ".cs", ".kt", ".java", ".rs", ".proto", ".h", ".json", ".yaml", ".yml"}:
            continue
        if path.stat().st_size > 4_194_304:
            errors.append({"source": relative, "reason": "over 4 MiB, not scanned"}); continue
        before = len(entries)
        try:
            raw = path.read_bytes(); text = raw.decode("utf-8-sig")
            if path.name == "plugin.manifest.json":
                manifest = json.loads(text)
                add("plugin_manifest", relative, catalog_candidate=len(Path(relative).parts) == 4 and Path(relative).parts[0] == "plugins", plugin_id=manifest.get("id"), version=manifest.get("version"), capabilities=manifest.get("capabilities"), methods=manifest.get("methods", []), runtime=manifest.get("runtime"), compatibility=manifest.get("compatibility"), state=manifest.get("state"))
            elif suffix == ".json" and ("schema" in path.name or "/contracts/" in f"/{relative}"):
                value = json.loads(text)
                if isinstance(value, dict) and ("$schema" in value or "$defs" in value):
                    add("json_schema", relative, title=value.get("title"), schema_id=value.get("$id"), definitions=list(value.get("$defs", {})))
            elif suffix in {".yaml", ".yml"} and ("openapi" in path.name.lower() or path.name == "capabilities.yaml"):
                value = yaml.safe_load(text)
                if isinstance(value, dict) and "openapi" in value:
                    for route, item in value.get("paths", {}).items():
                        for verb, op in item.items():
                            if verb.lower() in VERBS:
                                add("openapi_operation", relative, method=verb.upper(), path=route, operation_id=op.get("operationId"), security=op.get("security", value.get("security")), servers=value.get("servers", []))
                if isinstance(value, dict) and path.name == "capabilities.yaml":
                    for capability in value.get("capabilities", []):
                        add("capability_catalog", relative, declaration=capability)
            elif suffix == ".proto":
                # Source declarations only, not proof that a server is registered.
                clean = re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)
                package = re.search(r"\bpackage\s+([\w.]+)\s*;", clean)
                add("proto_contract", relative, package=package[1] if package else None, messages=re.findall(r"\bmessage\s+(\w+)\s*\{", clean), enums=re.findall(r"\benum\s+(\w+)\s*\{", clean))
                services = list(re.finditer(r"\bservice\s+(\w+)\s*\{", clean))
                for index, service in enumerate(services):
                    block = clean[service.end():services[index+1].start() if index+1 < len(services) else len(clean)]
                    for rpc in re.finditer(r"\brpc\s+(\w+)\s*\((.*?)\)\s*returns\s*\((.*?)\)", block, re.S):
                        add("proto_rpc", relative, package=package[1] if package else None, service=service[1], method=rpc[1], input=" ".join(rpc[2].split()), output=" ".join(rpc[3].split()))
            elif suffix == ".py" and not path.name.endswith("_pb2.py"):
                tree = ast.parse(text)
                constants = {}
                for node in tree.body:
                    if isinstance(node, ast.Assign):
                        value = route_literal(node.value, constants)
                        if isinstance(value, str):
                            for target in node.targets:
                                if isinstance(target, ast.Name): constants[target.id] = value
                prefixes = {}
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "APIRouter":
                        prefix = next((route_literal(k.value, constants) for k in node.value.keywords if k.arg == "prefix"), "")
                        for target in node.targets:
                            if isinstance(target, ast.Name): prefixes[target.id] = prefix
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for decorator in node.decorator_list:
                            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute): continue
                            verb = decorator.func.attr
                            if verb not in VERBS | {"route", "api_route", "websocket"} or not decorator.args: continue
                            route = route_literal(decorator.args[0], constants)
                            if not isinstance(route, str) or not route.startswith("/"):
                                add("unresolved_route", relative, line=decorator.lineno, expression=ast.unparse(decorator.args[0])[:240], method=verb, handler=node.name); continue
                            receiver = ast.unparse(decorator.func.value)
                            methods = next((literal(k.value) for k in decorator.keywords if k.arg == "methods"), [verb.upper()])
                            add("python_route_declaration", relative, line=decorator.lineno, receiver=receiver, router_prefix=prefixes.get(receiver), path=route, methods=methods, handler=node.name, mounting="inspect include_router/mount in provider entrypoint")
                    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"Route", "WebSocketRoute", "Mount"} and node.args:
                        route = literal(node.args[0])
                        if isinstance(route, str) and route.startswith("/"):
                            add("python_route_declaration", relative, line=node.lineno, path=route, methods=next((literal(k.value) for k in node.keywords if k.arg == "methods"), None), mounting="Starlette declaration; inspect mount tree")
            elif suffix in {".ts", ".tsx"}:
                if relative.startswith("packages/") and path.name in {"contracts.ts", "commands.ts"}:
                    for match in re.finditer(r'^\s*"([\w]+\.[\w_]+)"\s*:\s*\{\s*input:', text, re.M):
                        end = text.find("\n", match.end()); declaration = text[match.start():end if end >= 0 else len(text)]
                        scope = re.search(r'scope:\s*"([^\"]+)"', declaration)
                        add("client_command", relative, name=match[1], read_only="readOnly: true" in declaration, explicit_scope=scope[1] if scope else None, line=text.count("\n", 0, match.start())+1)
                if relative == "apps/control/application.ts":
                    for match in re.finditer(r'\["(/studio-[^\"]+)",\s*\{\s*commands', text):
                        add("client_http_group", relative, prefix=match[1], endpoints=["GET "+match[1]+"/v1/session", "POST "+match[1]+"/v1/commands"])
                    for match in re.finditer(r'(?:path|req\.url)\s*===\s*"(/[^\"]+)"', text):
                        line=text[text.rfind("\n",0,match.start())+1:text.find("\n",match.end())]
                        method=re.search(r'req\.method\s*===\s*"(\w+)"',line)
                        add("client_http_route", relative, path=match[1], method=method[1] if method else "see handler", line=text.count("\n",0,match.start())+1)
                if relative == "apps/mcp/server.ts":
                    for match in re.finditer(r'register(Resource|Prompt)\("([^\"]+)"(?:,\s*"([^\"]+)")?',text):
                        add("mcp_resource" if match[1] == "Resource" else "mcp_prompt", relative, name=match[2], uri=match[3])
                for match in re.finditer(r'export\s+interface\s+(\w+)',text):
                    if relative.startswith("packages/"): add("typescript_interface",relative,name=match[1],line=text.count("\n",0,match.start())+1)
            elif suffix == ".rs":
                for match in re.finditer(r'pub(?:\([^)]*\))?\s+(?:unsafe\s+)?trait\s+(\w+)',text):
                    add("rust_trait_declaration",relative,name=match[1],line=text.count("\n",0,match.start())+1,visibility=text[match.start():match.end()])
            elif suffix in {".cs", ".kt", ".java"}:
                for match in re.finditer(r'\bMap(Get|Post|Put|Patch|Delete|Methods|Group)\s*\(\s*"([^\"]+)"',text):
                    add("dotnet_route_declaration",relative,method=match[1].upper(),path=match[2],line=text.count("\n",0,match.start())+1,mounting="inspect MapGroup and host")
                for match in re.finditer(r'(?:@|\[)(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping|HttpGet|HttpPost|HttpPut|HttpDelete|Route)\s*\(\s*"([^\"]+)"',text):
                    add("attribute_route_declaration",relative,annotation=match[1],path=match[2],line=text.count("\n",0,match.start())+1)
            elif suffix == ".h":
                add("c_abi_header",relative,symbols=sorted(set(re.findall(r'\b((?:cyrene_|cy_)\w+)\s*\(',text))))
            if len(entries)>before: sources[relative]=hashlib.sha256(raw).hexdigest()
        except (ValueError, SyntaxError, UnicodeError, yaml.YAMLError) as error:
            errors.append({"source":relative,"reason":type(error).__name__})
    return {"name":name,"head":git(root,"rev-parse","HEAD"),"branch":git(root,"branch","--show-current"),"worktree_changes":worktree_change_count(root),"source_hashes":sources,"counts":dict(Counter(e["kind"] for e in entries)),"entries":entries,"scan_errors":errors}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo",action="append",required=True,help="Name=absolute checkout path; repeat for each repository")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    repos=[]
    for declaration in args.repo:
        name,path=declaration.split("=",1)
        if any(repo["name"]==name for repo in repos): parser.error("Duplicate repository name")
        repos.append(scan(name,Path(path)))
    result={"schema":"cyrene.local.interface-inventory.v1","observed_at":datetime.now(timezone.utc).isoformat(),"coverage":"Static source declaration index, not deployed endpoint or permission verification. Mounts, dynamic routing, binary SDKs and generated projections need targeted review. Declaration counts may include source mirrors; different categories are not additive unique APIs.","repositories":repos}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    for repo in repos: print(repo["name"],repo["head"][:12],repo["counts"],"scan_errors=",len(repo["scan_errors"]))

if __name__=="__main__": main()
