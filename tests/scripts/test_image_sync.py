# -*- coding: utf-8 -*-
"""Direct test of image sync flow"""
import asyncio
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")

async def main():
    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage

    settings = Settings()

    # Connect to database
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    print("=== 1. Getting Confluence connection ===")
    # Get connection from database
    connections = await db.list_confluence_connections()
    print(f"Connections found: {len(connections)}")
    if not connections:
        print("No connections found!")
        return

    conn = connections[0]
    connection_id = conn["connection_id"]
    print(f"Using connection: {conn['name']} ({connection_id})")

    print("\n=== 2. Getting binding ===")
    bindings = await db.list_confluence_bindings(connection_id=connection_id)
    print(f"Bindings found: {len(bindings)}")
    if not bindings:
        print("No bindings found!")
        return

    binding = bindings[0]
    binding_id = binding["binding_id"]
    print(f"Binding: {binding['space_key']}, sync_images={binding.get('sync_images')}")

    print("\n=== 3. Getting Confluence pages ===")
    pages = await db.list_confluence_pages(binding_id=binding_id, limit=5)
    print(f"Pages found: {len(pages)}")
    for p in pages:
        print(f"  - {p['title']} (page_id={p['page_id']})")

    print("\n=== 4. Testing Confluence Client - Get attachments ===")
    from src.services.knowledge.confluence.client import ConfluenceClient, ConfluenceCredentials

    credentials = ConfluenceCredentials(
        domain=conn["domain"],
        email=conn["email"],
        api_token=conn["api_token"],
    )
    client = ConfluenceClient(credentials)

    if pages:
        page = pages[0]
        page_id = page["page_id"]
        print(f"\nGetting attachments for page: {page['title']} ({page_id})")

        try:
            attachments = await client.get_page_image_attachments(
                page_id=page_id,
                embeddable_only=True
            )
            print(f"Image attachments found: {len(attachments)}")
            for att in attachments:
                print(f"  - {att.filename} ({att.media_type}, {att.file_size} bytes)")
        except Exception as e:
            print(f"Error getting attachments: {e}")
            import traceback
            traceback.print_exc()

    await client.close()
    await db.close()
    print("\nDone!")

if __name__ == "__main__":
    asyncio.run(main())
