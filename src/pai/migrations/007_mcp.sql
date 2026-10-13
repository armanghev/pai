-- Remote MCP servers used through the API's MCP connector. Tokens never leave the DB except
-- in the request to Anthropic; they're never shown or logged.
CREATE TABLE mcp_servers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    url        TEXT NOT NULL,
    token      TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE mcp_tools (
    id          INTEGER PRIMARY KEY,
    server_id   INTEGER NOT NULL REFERENCES mcp_servers (id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    read_only   INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (server_id, name)
);
