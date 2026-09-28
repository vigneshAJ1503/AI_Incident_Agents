"""The ``knowledge`` capability. knowledge-mcp's ``search``/``get_doc``/``list_docs`` are
already a neutral contract (plain-text queries, no query language), so the provider is
thin: it describes that contract for the registry and ``aiops profile validate``.
A Confluence provider (planned) would map its search API onto the same tools."""
