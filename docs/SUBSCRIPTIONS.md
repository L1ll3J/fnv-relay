# ChatGPT plans and connection checks

The relay calls no model API. Its Python polling, compiler checks and expiry guardians do not themselves use ChatGPT model tokens. The web conversation uses the account, model and tool limits applicable to that conversation.

There is no Pro-only feature in the relay. It needs a ChatGPT account whose selected conversation can use Notion tools to **read and write** the private relay pages. Plus and Free have not yet been validated end to end with this package. Do not buy a higher plan solely on a claim that it is required here.

OpenAI states that plugin availability depends on the plan, workspace settings and plugin. Check the actual account rather than assuming either that all Free accounts work or that Free accounts cannot use plugins. [Official plugin guidance](https://learn.chatgpt.com/docs/use-chatgpt)

## Chat versus Work

Plugins can be used in both Chat and Work where available. These are different execution modes. [Official plugin documentation](https://learn.chatgpt.com/docs/plugins)

OpenAI explicitly states that **ChatGPT Work and Codex share pricing, credits and usage limits**. Opening Work in a browser does not make it a separate unmetered allowance. A successful relay run is not proof of a particular billing treatment; inspect the account's usage and the mode actually selected. [Official pricing documentation](https://learn.chatgpt.com/docs/pricing)

Ordinary Chat is the mode used in our existing web relay setup. It still has its applicable model and tool limits. This package does not promise unlimited reasoning, unlimited turns or avoidance of subscription limits.

## Test an account before claiming work

1. In ChatGPT, install/connect Notion if the account offers it. Authorize a private test page belonging to you.
2. In the conversation mode you intend to use, ask ChatGPT to read that page, append a harmless unique test marker, and read the marker back using its Notion tools.
3. Open the page yourself and confirm that the marker really exists. A message in the chat saying it wrote something is not the test result.
4. If both reading and writing work, proceed with the setup wizard and one small lane. If only search/read tools are available, the unattended candidate/result workflow cannot work on that account yet.

This capability check needs no FNV claim, game data or model API key. It consumes the web account's ordinary interaction/tool allowance.

## Attribution

`ChatGPT Web via Notion` identifies the delivery route without guessing the underlying model. To supply a model name you can confirm in the UI:

```sh
fnv-relay lane create --model-label 'YOUR CONFIRMED MODEL via Web'
```

The setting is reported metadata, not a locally observed model measurement. Reasoning effort (for example Extra High), subscription tier and model name are different facts. The project's `model` field does not change which human FNV account owns the contribution.

Official OpenAI references checked 2026-10-24. Availability and billing rules can change.
