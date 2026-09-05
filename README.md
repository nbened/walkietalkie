# WalkieTalkie

Run local laptop agents from your phone.

Run commands, chat with agents, and view local servers from your phone through your Mac's cmux sessions.

## Quick start

On your Mac, with [cmux](https://cmux.com) running:

```sh
npx callwalkietalkie
```

Scan the QR code that opens in your browser. Pick a terminal or browser session on your phone and keep working.

The command uses our **free hosted relay at [callwalkietalkie.com](https://callwalkietalkie.com)** by default. No account, server setup, or repository clone is needed. Keep your Mac running and connected while you use it. Your existing agent subscriptions or API charges still apply.

## Requirements

- macOS with cmux running and socket access enabled.
- Node.js 18 or newer, including npm/npx.
- Python 3. The first run creates a Python environment and installs the runtime dependencies.
- A phone with a browser. No phone app installation is required.

## Features

- Send messages and commands to cmux terminal sessions.
- Read agent responses and terminal output, and interrupt a running agent.
- Switch between terminal and browser sessions.
- View live screenshots of cmux browser tabs, including local development servers open in those tabs.

## How it works

```text
Phone browser ↔ hosted relay ↔ Mac runtime ↔ cmux
```

Your Mac opens an outbound connection to the relay. The relay forwards requests from your phone to the Mac and returns its responses. Your agents and development servers run on your Mac.

This repository contains the open-source client: the CLI, Mac runtime, cmux adapter, and phone UI. The hosted relay is a separately operated service; its implementation and the marketing site are not included here.

## Options

```sh
npx callwalkietalkie --help
```

| Option | Description |
| --- | --- |
| `--port 8788` | Change the local server port (default: `8787`). |
| `--no-open` | Print the setup URL without opening a browser. |
| `--fresh` | Restart the local server. The saved machine key is reused. |
| `--version` | Print the installed version. |

The CLI stores its machine key and Python environment in `~/.callwalkietalkie`, or reuses an existing `~/.longleash` installation. Set `CALLWALKIETALKIE_HOME` to use a different directory.

## Using another relay

Using our hosted relay is the simplest way to get started. To use another provider, you need a compatible WalkieTalkie relay; an ordinary web server or generic WebSocket proxy is not sufficient.

```sh
CALLWALKIETALKIE_SITE=https://relay.example.com \
CALLWALKIETALKIE_RELAY=https://relay.example.com \
CALLWALKIETALKIE_HOME="$HOME/.walkietalkie-custom" \
npx callwalkietalkie --port 8788
```

`CALLWALKIETALKIE_SITE` selects the machine-key registration endpoint. `CALLWALKIETALKIE_RELAY` selects the traffic relay. The separate home directory creates a key with that provider, and the separate port avoids reusing an existing local server.

A compatible relay must support key registration at `POST /v1/keys`, the Mac's `/host` WebSocket protocol, and the `/pair/:code` and `/p/:code` phone routes. The client protocol is implemented in [the CLI](bin/callwalkietalkie.js) and [the Mac runtime](runtime/winproxy.py). A self-hostable relay implementation is not supplied in this repository.

## Development

```sh
git clone https://github.com/nbened/walkietalkie.git
cd walkietalkie
npm ci
npm run check
npm start -- --port 8788
```

| Path | Purpose |
| --- | --- |
| `bin/callwalkietalkie.js` | CLI, Python environment setup, and machine-key registration. |
| `runtime/winproxy.py` | Local HTTP server and outbound relay connection. |
| `runtime/agent_ui.html` | Phone interface. |
| `runtime/cmux-adapter/cmux_adapter.py` | cmux session discovery, terminal access, and browser screenshots. |
| `runtime/requirements.txt` | Python dependencies. |

Edit the files in `runtime/` directly; there is no generated runtime or sync step. Refresh the phone page for UI edits. Restart the local server after Python changes. Run `npm pack --dry-run` to inspect the package contents.

## Contributing

Issues and pull requests are welcome. Describe the problem, keep changes focused, and include how you checked them. Please leave machine keys, pairing links, and private terminal output out of issues and screenshots.

## License

[MIT](LICENSE). This license covers the code in this repository, not the separately operated hosted relay.
