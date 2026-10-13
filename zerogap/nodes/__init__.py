"""One module per former n8n node. Every node is `run(ctx, items) -> list[dict]`:
items = the JSON of the input items, ctx gives access to earlier node outputs (like n8n's $('Node name'))."""
