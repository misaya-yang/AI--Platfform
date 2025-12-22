import asyncio
import json
from src.persistence.database import DatabaseStorage

async def main():
    db_url = "postgresql://postgres:111111@localhost:5432/gateway"
    db = DatabaseStorage(dsn=db_url, enabled=True, auto_init=False)
    await db.connect()
    
    # 获取当前服务
    service = await db.get_service('hejaz-agent')
    if service:
        print("Current connector_config:", service.get('connector_config'))
        
        # 更新 connector_config
        new_config = {
            'base_url': 'http://localhost:8123',
            'graph_id': 'hejaz_agent'
        }
        
        # 更新服务
        service['connector_config'] = new_config
        await db.save_service(service)
        print("Updated connector_config to:", new_config)
        
        # 验证
        updated = await db.get_service('hejaz-agent')
        print("Verified connector_config:", updated.get('connector_config'))
    else:
        print("Service not found")
    
    await db.close()

asyncio.run(main())


