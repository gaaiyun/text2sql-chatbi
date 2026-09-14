"""构建站点：静态页面 + 浏览器端 Python 引擎 + 数据集 + 构建时预计算的数据。

    python scripts/build_site.py
    cd site && npx wrangler deploy

1. 复制 site/src，给静态资源打上构建号；
2. 打包 text2sql 的 wheel，下载与当前环境版本一致的 sqlglot wheel；
3. 准备 Pyodide 运行时，duckdb / pyyaml 轮子按 pyodide-lock.json 的 sha256 校验；
4. 生成合成演示库并 gzip；把开源数据集转成 Parquet（原始文件固定到提交并校验 sha256），写出示例数据；
5. 用同一个智能体预计算：评测结果、示例问题的完整运行结果、解析调试台默认问题；
6. 字体子集化：思源黑体只保留页面与数据卡片里用到的字，等宽字体只保留 ASCII。

外部下载都缓存在 T2S_SITE_CACHE（默认 ~/.cache/text2sql-site），重复构建不再联网。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "site" / "src"
DIST = ROOT / "site" / "dist"
sys.path.insert(0, str(ROOT))

PYODIDE_VERSION = "314.0.7"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
PYODIDE_CORE = (
    "pyodide.mjs",
    "pyodide.asm.mjs",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
)
PYODIDE_PACKAGES = ("duckdb", "pyyaml")
# 上传 Excel 时才按需加载
PYODIDE_OPTIONAL = {"excel": ("python-calamine", "packaging")}
FONT_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl"
FONTS = {
    "NotoSansSC-VF.ttf": f"{FONT_BASE}/notosanssc/NotoSansSC%5Bwght%5D.ttf",
    "NotoSansSC-OFL.txt": f"{FONT_BASE}/notosanssc/OFL.txt",
    "BigShouldersDisplay-VF.ttf": f"{FONT_BASE}/bigshouldersdisplay/BigShouldersDisplay%5Bwght%5D.ttf",
    "BigShouldersDisplay-OFL.txt": f"{FONT_BASE}/bigshouldersdisplay/OFL.txt",
    "ChivoMono-VF.ttf": f"{FONT_BASE}/chivomono/ChivoMono%5Bwght%5D.ttf",
    "ChivoMono-OFL.txt": f"{FONT_BASE}/chivomono/OFL.txt",
}
SHOWCASE_QUESTION = "各城市有融资记录的企业有多少家"
EXPLAIN_QUESTION = "2020年以来有融资记录的企业数量"
PAGES_FILE_LIMIT = 25 * 1024 * 1024


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def cache_dir() -> Path:
    path = Path(os.environ.get("T2S_SITE_CACHE") or Path.home() / ".cache" / "text2sql-site")
    path.mkdir(parents=True, exist_ok=True)
    return path


def fetch(url: str, target: Path, *, sha256: str | None = None) -> Path:
    if not (
        target.exists() and target.stat().st_size and (sha256 is None or digest(target) == sha256)
    ):
        log(f"下载 {url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as response:
            target.write_bytes(response.read())
    if sha256 is not None and digest(target) != sha256:
        raise SystemExit(f"校验失败：{target.name} 的 sha256 与 pyodide-lock.json 不一致")
    return target


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*command: str, cwd: Path = ROOT) -> str:
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


def build_id() -> tuple[str, str]:
    try:
        commit = run("git", "rev-parse", "--short", "HEAD").strip()
        dirty = bool(run("git", "status", "--porcelain").strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        commit, dirty = "nogit", True
    suffix = hashlib.sha1(str(os.times()).encode()).hexdigest()[:4] if dirty else ""
    return commit, f"{commit}{'-' + suffix if suffix else ''}"


# --------------------------------------------------------------------------- 各步骤


def copy_sources(build: str) -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(SRC, DIST)
    index = DIST / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("__BUILD__", build), encoding="utf-8"
    )


def build_wheels(build: str) -> list[dict]:
    target = DIST / "build" / build / "py"
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        run(sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-q", "-w", tmp)
        sqlglot_version = importlib.metadata.version("sqlglot")
        cached = cache_dir() / "wheels"
        cached.mkdir(exist_ok=True)
        if not list(cached.glob(f"sqlglot-{sqlglot_version}-*.whl")):
            run(
                sys.executable,
                "-m",
                "pip",
                "download",
                f"sqlglot=={sqlglot_version}",
                "--no-deps",
                "-q",
                "-d",
                str(cached),
            )
        wheels = [
            *Path(tmp).glob("text2sql_analysis-*.whl"),
            *cached.glob(f"sqlglot-{sqlglot_version}-*.whl"),
        ]
        for wheel in wheels:
            shutil.copy2(wheel, target / wheel.name)
    return [
        {
            "name": wheel.name,
            "url": f"/build/{build}/py/{wheel.name}",
            "bytes": wheel.stat().st_size,
        }
        for wheel in sorted(target.iterdir())
    ]


def prepare_pyodide() -> dict:
    source = cache_dir() / "pyodide" / PYODIDE_VERSION
    target = DIST / "pyodide" / PYODIDE_VERSION
    target.mkdir(parents=True, exist_ok=True)
    for name in PYODIDE_CORE:
        shutil.copy2(fetch(PYODIDE_CDN + name, source / name), target / name)
    lock = json.loads((source / "pyodide-lock.json").read_text(encoding="utf-8"))
    bundled = set(PYODIDE_PACKAGES) | {p for group in PYODIDE_OPTIONAL.values() for p in group}
    for package in sorted(bundled):
        entry = lock["packages"][package]
        missing = set(entry.get("depends") or []) - bundled
        if missing:
            raise SystemExit(f"{package} 依赖 {sorted(missing)}，需要一并纳入站点")
        wheel = fetch(
            PYODIDE_CDN + entry["file_name"], source / entry["file_name"], sha256=entry["sha256"]
        )
        shutil.copy2(wheel, target / wheel.name)
    return {
        "version": PYODIDE_VERSION,
        "module": f"/pyodide/{PYODIDE_VERSION}/pyodide.mjs",
        "indexURL": f"/pyodide/{PYODIDE_VERSION}/",
        "packages": list(PYODIDE_PACKAGES),
        "optional": {name: list(group) for name, group in PYODIDE_OPTIONAL.items()},
        "python": lock["info"]["python"],
        "duckdb": lock["packages"]["duckdb"]["version"],
    }


def build_database(build: str) -> dict:
    from text2sql.db.demo import build_demo_database

    target = DIST / "build" / build / "data"
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        path = build_demo_database(Path(tmp) / "znjz_demo.duckdb")
        raw = path.read_bytes()
    packed = gzip.compress(raw, compresslevel=9, mtime=0)
    (target / "znjz_demo.duckdb.gz").write_bytes(packed)
    log(f"演示库 {len(raw) / 1048576:.1f} MB，gzip 后 {len(packed) / 1048576:.1f} MB")
    return {
        "url": f"/build/{build}/data/znjz_demo.duckdb.gz",
        "bytes": len(packed),
        "raw_bytes": len(raw),
        "gzip": True,
    }


DATASET_ORDER = ("retail", "northwind", "chinook", "gapminder", "owid_co2")


def build_datasets(build: str) -> tuple[dict, list[dict]]:
    """返回 (manifest 里的文件清单, site.json 里的数据集目录)。"""
    from scripts.opendata import build as build_open_data
    from text2sql.datasets.cards import load_card
    from text2sql.datasets.samples import write_retail_sample

    target = DIST / "build" / build / "datasets"
    entries = build_open_data(target, cache_dir() / "opendata")
    retail = []
    for path in write_retail_sample(target / "retail"):
        rows = path.read_text(encoding="utf-8").count("\n") - 1
        retail.append(
            {
                "table": path.stem,
                "file": f"retail/{path.name}",
                "rows": rows,
                "bytes": path.stat().st_size,
            }
        )
    entries["retail"] = retail

    manifest, catalog = {}, []
    for dataset in DATASET_ORDER:
        card = load_card(dataset)
        files = entries[dataset]
        manifest[dataset] = [
            {
                "name": Path(f["file"]).name,
                "url": f"/build/{build}/datasets/{f['file']}",
                "bytes": f["bytes"],
            }
            for f in files
        ]
        info = card.public()
        info["tables"] = [
            {
                "name": f["table"],
                "label": card.tables.get(f["table"], {}).get("label", f["table"]),
                "rows": f["rows"],
            }
            for f in files
        ]
        info["rows"] = sum(f["rows"] for f in files)
        info["bytes"] = sum(f["bytes"] for f in files)
        catalog.append(info)
        log(
            f"数据集 {dataset}：{len(files)} 张表，{info['rows']} 行，{info['bytes'] / 1024:.0f} KB"
        )
    return manifest, catalog


def count_tests() -> int:
    # pyproject 的 addopts 已带 -q，再加 -q 时 pytest 只输出“文件: 数量”，两种格式都兼容
    output = run(sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider")
    match = re.search(r"(\d+) tests? collected", output)
    if match:
        return int(match.group(1))
    return sum(int(n) for n in re.findall(r"^tests[\\/][^:]+:\s*(\d+)\s*$", output, re.MULTILINE))


def precompute(commit: str, pyodide: dict) -> dict:
    from dataclasses import asdict

    from text2sql import __version__
    from text2sql.agent.graph import CONDITIONAL_EDGES, FIXED_EDGES, NODE_ORDER, Text2SQLAgent
    from text2sql.config import Settings
    from text2sql.db.demo import DEMO_ENTERPRISES
    from text2sql.db.schema import load_znjz_schema
    from text2sql.evaluation.runner import load_benchmark, run_evaluation
    from text2sql.semantic.explain import explain_question

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.from_mapping(
            {"T2S_DATABASE": "demo", "T2S_DEMO_DB_PATH": str(Path(tmp) / "demo.duckdb")}
        )
        agent = Text2SQLAgent.from_settings(settings, llm=None, planners=("semantic",))
        try:
            log("预计算评测")
            summary, outcomes = run_evaluation(
                agent, load_benchmark(), backend=agent.backend, mode="semantic"
            )
            keep = (
                "id",
                "question",
                "category",
                "expect",
                "status",
                "correct",
                "refused_ok",
                "reason",
                "latency_ms",
            )
            evaluation = {
                "summary": summary.to_dict(),
                "outcomes": [{k: v for k, v in asdict(o).items() if k in keep} for o in outcomes],
            }
            snapshot = agent.ask(SHOWCASE_QUESTION).to_dict()
            explain = explain_question(
                EXPLAIN_QUESTION,
                parser=agent.parser,
                catalog=agent.catalog,
                guard=agent.guard,
                max_rows=settings.max_rows,
            )
            catalog = agent.catalog.summary()
        finally:
            agent.close()

    schema = load_znjz_schema()
    views = sum(1 for table in schema.tables.values() if table.kind == "view")
    version = importlib.metadata.version
    return {
        "build": {
            "version": __version__,
            "commit": commit,
            "date": date.today().isoformat(),
            "tests": count_tests(),
            "enterprises": DEMO_ENTERPRISES,
            "tables": len(schema.tables) - views,
            "views": views,
            "stack": [
                ["Python", f"3.11–3.14（浏览器 {pyodide['python']}）"],
                ["LangGraph", version("langgraph")],
                ["sqlglot", version("sqlglot")],
                ["DuckDB", f"{version('duckdb')} / WASM {pyodide['duckdb']}"],
                ["FastAPI", version("fastapi")],
                ["MCP SDK", version("mcp")],
                ["Streamlit", version("streamlit")],
                ["Pyodide", pyodide["version"]],
                ["Cloudflare Workers AI", ""],
            ],
        },
        "routes": {
            "nodes": list(NODE_ORDER),
            "fixed": FIXED_EDGES,
            "conditional": {k: list(v) for k, v in CONDITIONAL_EDGES.items()},
        },
        "catalog": catalog,
        "examples": catalog["examples"],
        "evaluation": evaluation,
        "snapshot": snapshot,
        "explain": explain,
    }


def subset_fonts() -> None:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    fonts = cache_dir() / "fonts"
    for name, url in FONTS.items():
        fetch(url, fonts / name)
    target = DIST / "assets" / "fonts"
    target.mkdir(parents=True, exist_ok=True)

    cards = ROOT / "text2sql" / "datasets" / "cards"
    static_text = [
        SRC / "index.html",
        *sorted((SRC / "assets" / "js").glob("*.js")),
        *sorted(cards.glob("*.yaml")),
    ]
    chars = set("0123456789%.,/·—–-：、（）()·×→↗ ")
    for path in static_text:
        chars.update(ch for ch in path.read_text(encoding="utf-8") if ord(ch) > 0x2E7F)
    cjk_text = "".join(sorted(chars))
    ascii_text = "".join(chr(c) for c in range(0x20, 0x7F)) + "·—–→←↗…×▸"

    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["*"]
    options.hinting = False
    options.desubroutinize = True

    def save(font: TTFont, text: str, filename: str) -> int:
        subsetter = subset.Subsetter(options)
        subsetter.populate(text=text)
        subsetter.subset(font)
        font.flavor = "woff2"
        font.save(target / filename)
        return (target / filename).stat().st_size // 1024

    # 标题只用到页面里的静态文字：思源黑体 Black 按字子集化
    heavy = instancer.instantiateVariableFont(TTFont(fonts / "NotoSansSC-VF.ttf"), {"wght": 900})
    heavy_kb = save(heavy, cjk_text, "heavy.woff2")
    signage = instancer.instantiateVariableFont(
        TTFont(fonts / "BigShouldersDisplay-VF.ttf"), {"wght": 800}
    )
    save(signage, ascii_text, "signage.woff2")
    for weight, filename in ((400, "mono-regular.woff2"), (700, "mono-bold.woff2")):
        mono = instancer.instantiateVariableFont(
            TTFont(fonts / "ChivoMono-VF.ttf"), {"wght": weight}
        )
        save(mono, ascii_text, filename)
    for license_name in ("NotoSansSC-OFL.txt", "BigShouldersDisplay-OFL.txt", "ChivoMono-OFL.txt"):
        shutil.copy2(fonts / license_name, target / license_name)
    log(f"字体子集：思源黑体 {len(cjk_text)} 字，{heavy_kb} KB")


def check_limits() -> None:
    total = 0
    for path in DIST.rglob("*"):
        if path.is_file():
            size = path.stat().st_size
            total += size
            if size > PAGES_FILE_LIMIT:
                raise SystemExit(
                    f"{path.relative_to(DIST)} 超过 Cloudflare 静态资源单文件 25 MB 上限"
                )
    count = sum(1 for p in DIST.rglob("*") if p.is_file())
    log(f"输出 {count} 个文件，共 {total / 1048576:.1f} MB → {DIST}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-fonts", action="store_true", help="跳过字体子集化（沿用系统字体）")
    args = parser.parse_args()

    commit, build = build_id()
    log(f"构建号 {build}")
    copy_sources(build)
    wheels = build_wheels(build)
    pyodide = prepare_pyodide()
    database = build_database(build)
    dataset_files, datasets = build_datasets(build)
    site = precompute(commit, pyodide)
    site["datasets"] = datasets
    data = DIST / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "site.json").write_text(
        json.dumps(site, ensure_ascii=False, default=str), encoding="utf-8"
    )
    manifest = {
        "build": build,
        "pyodide": pyodide,
        "wheels": wheels,
        "database": database,
        "datasets": dataset_files,
    }
    (data / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not args.skip_fonts:
        subset_fonts()
    check_limits()


if __name__ == "__main__":
    main()
