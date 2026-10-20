# Security

Do not include bot tokens, API keys, LinkedIn sessions, databases, logs, or application notes in issues or pull requests.

The API is designed for a trusted host or SSH tunnel. Keep its ports on loopback and use the API key header. The Telegram bot is restricted to one configured owner in a private chat.

For a suspected vulnerability, use GitHub private vulnerability reporting if enabled. Never post credentials publicly. Rotate an exposed bot token or API key, and replace an exposed LinkedIn session.
# Private web dashboard

The dashboard service binds only to server loopback (`127.0.0.1:8780`). Use an SSH forward or a trusted private network. A future internet-facing deployment needs HTTPS and `DASHBOARD_SECURE_COOKIE=1`; do not publish the raw HTTP port.

The dashboard access key is generated separately in `dashboard-data/access-key`, with owner-only permissions. API credentials remain server-side. Browser sessions use random tokens stored as hashes, expire after seven days, and are revoked on sign-out. Mutations require a custom request header and a matching Origin when present. Login attempts are rate-limited.

Keep the entire `dashboard-data/` directory and its backups private. It contains session state and personal job-triage choices. `DASHBOARD_DEMO=1` deliberately bypasses authentication and uses fictional fixtures; only enable it in an isolated, temporary loopback preview. Production Compose does not enable demo mode.

## Public employer feeds

The standard deployment disables LinkedIn collection and uses a browser-free API plus a separate public-feed worker. No source adapter loads LinkedIn cookies. Outbound feed requests use fixed provider hosts, validated board names, no redirects, finite timeouts, bounded responses, and durable budgets. The email preview parser performs no network requests and is not a mailbox connector.

Keep old LinkedIn session files outside all mounted runtime directories. Runtime databases and credentials remain excluded from Git and Docker build contexts. Dashboard mutations retain session authentication and same-origin CSRF checks; upstream source endpoints require the API key.
