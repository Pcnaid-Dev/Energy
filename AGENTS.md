# Repository rules

Any change to markup or CSS must add or update its states in `ui-states.json` and ship refreshed baselines in `tests/visual/__screenshots__/`.

The Grok visual audit uses Azure AI Foundry's OpenAI-compatible chat-completions endpoint with `grok-4.6`. It replaced GitHub Models because GitHub Models retired on 2026-07-30.

The Grok visual audit requires these repository secrets:

- `AZURE_VISION_ENDPOINT`: the full `.../openai/v1/chat/completions` URL.
- `AZURE_VISION_KEY`: the Azure AI Foundry API key.
- `AZURE_VISION_MODEL`: `grok-4.6`.
