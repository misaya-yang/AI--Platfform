"""Test Auto Finance FAQs page image sync"""

import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")


async def main():
    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.confluence.client import ConfluenceClient, ConfluenceCredentials

    settings = Settings()
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    print("=== Finding HFDSH binding ===")
    # Find HFDSH binding
    all_bindings = []
    connections = await db.list_confluence_connections()
    for conn in connections:
        bindings = await db.list_confluence_bindings(connection_id=conn["connection_id"])
        for b in bindings:
            b["_connection"] = conn
            all_bindings.append(b)

    hfdsh_binding = None
    for b in all_bindings:
        if b["space_key"] == "HFDSH":
            hfdsh_binding = b
            break

    if not hfdsh_binding:
        print("HFDSH binding not found!")
        return

    print(f"Found HFDSH binding: {hfdsh_binding['binding_id']}")
    print(f"  sync_images: {hfdsh_binding.get('sync_images')}")
    conn = hfdsh_binding["_connection"]

    print("\n=== Finding Auto Finance FAQs page ===")
    pages = await db.list_confluence_pages(binding_id=hfdsh_binding["binding_id"], limit=50)
    print(f"Total pages: {len(pages)}")

    auto_finance_page = None
    for p in pages:
        if "Auto Finance FAQs" in p["title"]:
            auto_finance_page = p
            print(f"Found: {p['title']} (page_id={p['page_id']})")
            break

    if not auto_finance_page:
        print("Auto Finance FAQs page not found!")
        # List all pages
        for p in pages[:10]:
            print(f"  - {p['title']}")
        return

    print("\n=== Getting page attachments ===")
    credentials = ConfluenceCredentials(
        domain=conn["domain"],
        email=conn["email"],
        api_token=conn["api_token"],
    )
    client = ConfluenceClient(credentials)

    page_id = auto_finance_page["page_id"]
    attachments = await client.get_page_image_attachments(page_id=page_id, embeddable_only=True)
    print(f"Image attachments: {len(attachments)}")
    for att in attachments:
        print(f"  - {att.filename} ({att.media_type}, {att.file_size} bytes)")

    if attachments:
        print("\n=== Testing image download ===")
        att = attachments[0]
        print(f"Downloading: {att.filename}")
        content = await client.download_attachment(att)
        print(f"Downloaded: {len(content)} bytes")

        print("\n=== Testing VLM description ===")
        from src.services.knowledge.vlm_service import DashScopeVLMService

        vlm = DashScopeVLMService(
            api_key=settings.knowledge.dashscope.api_key,
            model="qwen-vl-max",
        )

        print("Generating VLM description...")
        result = await vlm.describe_image(
            image_bytes=content,
            image_type="table",
            context="Auto Finance FAQs - Fee table",
        )
        print(f"Description ({len(result.description)} chars):")
        print(result.description[:500])
        print("...")

    await client.close()
    await db.close()
    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
