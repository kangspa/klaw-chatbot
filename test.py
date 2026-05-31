import asyncio
from api.load_variable import MCP_CLIENT, LAW_TOOLS


async def inspect_mcp_server(server_name: str = "korean_law"):
    print("=" * 60)
    print(f"[Tools] 로드된 법률 도구: {len(LAW_TOOLS)}개")
    print("=" * 60)
    for t in LAW_TOOLS:
        print(f"  - {t.name}")

    async with MCP_CLIENT.session(server_name) as session:
        # --- Prompts ---
        print()
        print("=" * 60)
        print("[Prompts] 서버가 노출한 프롬프트 템플릿")
        print("=" * 60)
        try:
            prompts_result = await session.list_prompts()
            prompts = prompts_result.prompts
            if not prompts:
                print("  (없음)")
            else:
                for p in prompts:
                    print(f"  - {p.name}")
                    if p.description:
                        print(f"      desc: {p.description}")
                    if p.arguments:
                        for arg in p.arguments:
                            req = "필수" if arg.required else "선택"
                            print(f"      arg: {arg.name} ({req}) - {arg.description or ''}")
        except Exception as e:
            print(f"  list_prompts() 실패: {type(e).__name__}: {e}")

        # --- Resources (정적) ---
        print()
        print("=" * 60)
        print("[Resources] 서버가 노출한 정적 리소스")
        print("=" * 60)
        try:
            resources_result = await session.list_resources()
            resources = resources_result.resources
            if not resources:
                print("  (없음)")
            else:
                for r in resources:
                    print(f"  - {r.uri}")
                    if r.name:
                        print(f"      name: {r.name}")
                    if r.description:
                        print(f"      desc: {r.description}")
                    if r.mimeType:
                        print(f"      mime: {r.mimeType}")
        except Exception as e:
            print(f"  list_resources() 실패: {type(e).__name__}: {e}")

        # --- Resource Templates (URI 패턴 기반 동적 리소스) ---
        print()
        print("=" * 60)
        print("[Resource Templates] URI 패턴으로 접근 가능한 동적 리소스")
        print("=" * 60)
        try:
            templates_result = await session.list_resource_templates()
            templates = templates_result.resourceTemplates
            if not templates:
                print("  (없음)")
            else:
                for tpl in templates:
                    print(f"  - {tpl.uriTemplate}")
                    if tpl.name:
                        print(f"      name: {tpl.name}")
                    if tpl.description:
                        print(f"      desc: {tpl.description}")
                    if tpl.mimeType:
                        print(f"      mime: {tpl.mimeType}")
        except Exception as e:
            print(f"  list_resource_templates() 실패: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(inspect_mcp_server())
