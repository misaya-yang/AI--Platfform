#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class ParamSpec:
    name: str
    location: str
    required: bool
    description: str
    example: str
    default: str | None = None


@dataclass(frozen=True)
class EndpointSpec:
    name: str
    path_template: str
    description: str
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)


ENDPOINTS: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        name="health",
        path_template="/health",
        description="基础健康检查，不需要参数。",
    ),
    EndpointSpec(
        name="health_live",
        path_template="/health/live",
        description="进程存活检查，不需要参数。",
    ),
    EndpointSpec(
        name="health_ready",
        path_template="/health/ready",
        description="就绪检查，会验证数据库、缓存和模块数据是否 ready。",
    ),
    EndpointSpec(
        name="meta_config",
        path_template="/api/v1/meta/config",
        description="返回服务默认配置、启用模块和公共接口映射。",
    ),
    EndpointSpec(
        name="meta_manifest",
        path_template="/api/v1/meta/manifest",
        description="返回最近一次同步清单，不需要参数。",
    ),
    EndpointSpec(
        name="meta_canonical_summary",
        path_template="/api/v1/meta/canonical-summary",
        description="返回 canonical 表的行数统计，不需要参数。",
    ),
    EndpointSpec(
        name="quran_chapters",
        path_template="/api/v1/quran/chapters",
        description="列出全部 Quran chapters，不需要参数。",
    ),
    EndpointSpec(
        name="quran_translations",
        path_template="/api/v1/quran/resources/translations",
        description="列出可用 translation 元数据，不需要参数。",
    ),
    EndpointSpec(
        name="quran_recitations",
        path_template="/api/v1/quran/resources/recitations",
        description="列出可用 recitation 元数据，不需要参数。",
    ),
    EndpointSpec(
        name="quran_chapter_ayahs",
        path_template="/api/v1/quran/chapters/{chapter_id}/ayahs",
        description="返回整章 ayahs，含 chapter audio 元数据。",
        params=(
            ParamSpec(
                name="chapter_id",
                location="path",
                required=True,
                description="Quran chapter 编号，范围 1-114。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_audio_text",
        path_template="/api/v1/quran/chapters/{chapter_id}/audio-text",
        description="一体接口：整章文本 + chapter audio + verse timing + word segment。",
        params=(
            ParamSpec(
                name="chapter_id",
                location="path",
                required=True,
                description="Quran chapter 编号，范围 1-114。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_triplets",
        path_template="/api/v1/quran/chapters/{chapter_id}/triplets",
        description="三段式接口：每 3 ayah 一组，适合 AI Quran 模块。",
        params=(
            ParamSpec(
                name="chapter_id",
                location="path",
                required=True,
                description="Quran chapter 编号，范围 1-114。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_ayah_detail",
        path_template="/api/v1/quran/ayahs/{verse_key}",
        description="最小粒度 ayah 详情，含 words、timing、audio。",
        params=(
            ParamSpec(
                name="verse_key",
                location="path",
                required=True,
                description="经文定位，格式 `{surah}:{ayah}`，例如 `1:1`。",
                example="1:1",
                default="1:1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_ayah_minimal",
        path_template="/api/v1/quran/ayahs/{verse_key}/minimal",
        description="最小三段式接口：只返回阿拉伯文、读音、英文翻译，不带 words、timing、chapter audio。",
        params=(
            ParamSpec(
                name="verse_key",
                location="path",
                required=True,
                description="经文定位，格式 `{surah}:{ayah}`，例如 `1:1`。",
                example="1:1",
                default="1:1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_ayah_translation",
        path_template="/api/v1/quran/ayahs/{verse_key}/translation",
        description="单节 ayah translation-only 接口。",
        params=(
            ParamSpec(
                name="verse_key",
                location="path",
                required=True,
                description="经文定位，格式 `{surah}:{ayah}`，例如 `1:1`。",
                example="1:1",
                default="1:1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_chapter_translations",
        path_template="/api/v1/quran/chapters/{chapter_id}/translations",
        description="整章 translation-only 接口。",
        params=(
            ParamSpec(
                name="chapter_id",
                location="path",
                required=True,
                description="Quran chapter 编号，范围 1-114。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="translation_id",
                location="query",
                required=False,
                description="已同步的 translation 资源 ID，默认不传时走服务默认翻译。",
                example="20",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_chapter_audio",
        path_template="/api/v1/quran/chapters/{chapter_id}/audio",
        description="整章 chapter audio 与 verse/word timing 接口。",
        params=(
            ParamSpec(
                name="chapter_id",
                location="path",
                required=True,
                description="Quran chapter 编号，范围 1-114。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="recitation_id",
                location="query",
                required=False,
                description="已同步的 recitation 资源 ID，默认不传时走服务默认音色。",
                example="7",
            ),
        ),
    ),
    EndpointSpec(
        name="quran_user_auth_config",
        path_template="/api/v1/quran/user/auth/config",
        description="查看 Quran 用户 OAuth 和 User API 配置。",
    ),
    EndpointSpec(
        name="quran_user_authorize_url",
        path_template="/api/v1/quran/user/auth/authorize-url",
        description="生成 Quran 用户登录授权 URL（适用于 PKCE）。",
        params=(
            ParamSpec(
                name="redirect_uri",
                location="query",
                required=False,
                description="OAuth 回调地址，未传则使用服务配置里的 redirect_uri。",
                example="https://wahda.example/callback",
            ),
            ParamSpec(
                name="state",
                location="query",
                required=False,
                description="可选 OAuth state。",
                example="abc123",
            ),
            ParamSpec(
                name="code_challenge",
                location="query",
                required=False,
                description="PKCE code_challenge。",
                example="example_challenge",
            ),
        ),
    ),
    EndpointSpec(
        name="hadith_collections",
        path_template="/api/v1/hadith/collections",
        description="列出 Hadith collections，不需要参数。当前若 Hadith 模块未启用，会返回 503。",
    ),
    EndpointSpec(
        name="hadith_books",
        path_template="/api/v1/hadith/collections/{collection_name}/books",
        description="列出 collection 下的 books。",
        params=(
            ParamSpec(
                name="collection_name",
                location="path",
                required=True,
                description="Hadith collection 名称，例如 `bukhari`。",
                example="bukhari",
                default="bukhari",
            ),
        ),
    ),
    EndpointSpec(
        name="hadith_book_items",
        path_template="/api/v1/hadith/collections/{collection_name}/books/{book_number}/hadiths",
        description="列出某个 book 的 hadith 列表。",
        params=(
            ParamSpec(
                name="collection_name",
                location="path",
                required=True,
                description="Hadith collection 名称，例如 `bukhari`。",
                example="bukhari",
                default="bukhari",
            ),
            ParamSpec(
                name="book_number",
                location="path",
                required=True,
                description="Book 编号，字符串形式，示例 `1`。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="page",
                location="query",
                required=False,
                description="分页页码，最小 1。",
                example="1",
                default="1",
            ),
            ParamSpec(
                name="limit",
                location="query",
                required=False,
                description="分页大小，范围 1-200。",
                example="50",
                default="50",
            ),
        ),
    ),
    EndpointSpec(
        name="hadith_detail",
        path_template="/api/v1/hadith/collections/{collection_name}/hadiths/{hadith_number}",
        description="获取单条 Hadith 详情。",
        params=(
            ParamSpec(
                name="collection_name",
                location="path",
                required=True,
                description="Hadith collection 名称，例如 `bukhari`。",
                example="bukhari",
                default="bukhari",
            ),
            ParamSpec(
                name="hadith_number",
                location="path",
                required=True,
                description="Hadith 编号，字符串形式，示例 `1`。",
                example="1",
                default="1",
            ),
        ),
    ),
)

ENDPOINT_INDEX = {endpoint.name: endpoint for endpoint in ENDPOINTS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Islamic Content Service 测试脚本。"
            "可以列出每个接口需要的参数，也可以直接发请求验证返回。"
        )
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8091",
        help="服务根地址，默认 http://127.0.0.1:8091",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="打印全部接口、所需参数和示例，不发请求。",
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        choices=sorted(ENDPOINT_INDEX),
        help="指定要调用的 endpoint name，可重复传入多个。",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="按推荐顺序跑一组常用 smoke tests。",
    )
    parser.add_argument(
        "--include-hadith",
        action="store_true",
        help="smoke 模式下包含 Hadith 接口。Hadith 未启用时会返回 503。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="将每个接口响应写入目录，文件名按 endpoint name 命名。",
    )
    parser.add_argument("--chapter-id", default="1", help="chapter_id path 参数，默认 1")
    parser.add_argument("--verse-key", default="1:1", help="verse_key path 参数，默认 1:1")
    parser.add_argument("--translation-id", help="translation_id query 参数，默认不传")
    parser.add_argument("--recitation-id", help="recitation_id query 参数，默认不传")
    parser.add_argument("--redirect-uri", help="redirect_uri query 参数，默认不传")
    parser.add_argument("--state", help="OAuth state query 参数，默认不传")
    parser.add_argument("--code-challenge", help="PKCE code_challenge query 参数，默认不传")
    parser.add_argument(
        "--collection-name",
        default="bukhari",
        help="collection_name path 参数，默认 bukhari",
    )
    parser.add_argument("--book-number", default="1", help="book_number path 参数，默认 1")
    parser.add_argument("--hadith-number", default="1", help="hadith_number path 参数，默认 1")
    parser.add_argument("--page", default="1", help="page query 参数，默认 1")
    parser.add_argument("--limit", default="50", help="limit query 参数，默认 50")
    return parser


def list_endpoints() -> None:
    print("Available endpoints and required parameters:\n")
    for endpoint in ENDPOINTS:
        print(f"- {endpoint.name}")
        print(f"  path: {endpoint.path_template}")
        print(f"  desc: {endpoint.description}")
        if not endpoint.params:
            print("  params: none")
            continue
        print("  params:")
        for param in endpoint.params:
            default = f", default={param.default}" if param.default is not None else ""
            required = "required" if param.required else "optional"
            print(
                f"    - {param.name} [{param.location}, {required}{default}]"
                f" -> {param.description} | example={param.example}"
            )
        print()


def smoke_endpoints(include_hadith: bool) -> list[str]:
    names = [
        "health",
        "health_live",
        "health_ready",
        "meta_config",
        "meta_manifest",
        "meta_canonical_summary",
        "quran_chapters",
        "quran_translations",
        "quran_recitations",
        "quran_chapter_ayahs",
        "quran_audio_text",
        "quran_triplets",
        "quran_ayah_detail",
        "quran_ayah_minimal",
        "quran_ayah_translation",
        "quran_chapter_translations",
        "quran_chapter_audio",
        "quran_user_auth_config",
        "quran_user_authorize_url",
    ]
    if include_hadith:
        names.extend(
            [
                "hadith_collections",
                "hadith_books",
                "hadith_book_items",
                "hadith_detail",
            ]
        )
    return names


def build_path(endpoint: EndpointSpec, args: argparse.Namespace) -> str:
    values = {
        "chapter_id": args.chapter_id,
        "verse_key": args.verse_key,
        "collection_name": args.collection_name,
        "book_number": args.book_number,
        "hadith_number": args.hadith_number,
        "translation_id": args.translation_id,
        "recitation_id": args.recitation_id,
        "redirect_uri": args.redirect_uri,
        "state": args.state,
        "code_challenge": args.code_challenge,
    }
    path = endpoint.path_template
    for key, value in values.items():
        path = path.replace(f"{{{key}}}", str(value))
    return path


def build_query(endpoint: EndpointSpec, args: argparse.Namespace) -> dict[str, str]:
    query: dict[str, str] = {}
    for param in endpoint.params:
        if param.location != "query":
            continue
        value = getattr(args, param.name.replace("-", "_"))
        if value is not None:
            query[param.name] = str(value)
    return query


def summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = ", ".join(sorted(payload.keys())[:10])
        return f"dict keys=[{keys}]"
    if isinstance(payload, list):
        return f"list size={len(payload)}"
    return type(payload).__name__


def write_output(output_dir: Path, endpoint_name: str, payload: Any) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{endpoint_name}.json"
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_endpoint(
    client: httpx.Client,
    base_url: str,
    endpoint: EndpointSpec,
    args: argparse.Namespace,
) -> tuple[int, Any]:
    path = build_path(endpoint, args)
    query = build_query(endpoint, args)
    response = client.get(f"{base_url.rstrip('/')}{path}", params=query)
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = response.text
    print(f"\n== {endpoint.name} ==")
    print(f"GET {path}")
    if query:
        print(f"query={query}")
    print(f"status={response.status_code}")
    print(f"summary={summarize_payload(payload)}")
    if isinstance(payload, (dict, list)):
        preview = json.dumps(payload, ensure_ascii=False, indent=2)[:1200]
        print(preview)
    else:
        print(str(payload)[:1200])
    return response.status_code, payload


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list:
        list_endpoints()
        return 0

    endpoint_names = args.endpoint or []
    if args.smoke:
        endpoint_names.extend(smoke_endpoints(args.include_hadith))
    if not endpoint_names:
        parser.error("请传 --list 或 --smoke，或至少一个 --endpoint")

    seen: set[str] = set()
    ordered_names = [name for name in endpoint_names if not (name in seen or seen.add(name))]

    with httpx.Client(timeout=30.0) as client:
        failures = 0
        for endpoint_name in ordered_names:
            endpoint = ENDPOINT_INDEX[endpoint_name]
            status_code, payload = run_endpoint(client, args.base_url, endpoint, args)
            if args.output_dir and isinstance(payload, (dict, list)):
                write_output(args.output_dir, endpoint.name, payload)
            if status_code >= 400:
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
