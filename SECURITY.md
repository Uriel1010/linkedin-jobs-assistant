# Security

Do not include bot tokens, API keys, LinkedIn sessions, databases, logs, or application notes in issues or pull requests.

The API is designed for a trusted host or SSH tunnel. Keep its ports on loopback and use the API key header. The Telegram bot is restricted to one configured owner in a private chat.

For a suspected vulnerability, use GitHub private vulnerability reporting if enabled. Never post credentials publicly. Rotate an exposed bot token or API key, and replace an exposed LinkedIn session.
