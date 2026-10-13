-- Linking from the phone: access tokens Pai exchanged itself, stored encrypted with a key
-- from PAI_TOKEN_KEY, and links still waiting for the owner to finish.
ALTER TABLE money_items ADD COLUMN access_token TEXT;

CREATE TABLE money_links (
    link_token TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
