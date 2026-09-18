from dotenv import load_dotenv
load_dotenv()

import mcp_keys_db

mcp_keys_db.setup_mcp_tables()  # safe, does nothing if already created
result = mcp_keys_db.create_api_key("Test Customer")
print(result)