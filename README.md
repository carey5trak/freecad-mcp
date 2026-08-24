[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/neka-nat-freecad-mcp-badge.png)](https://mseep.ai/app/neka-nat-freecad-mcp)

# FreeCAD MCP

This repository is a FreeCAD MCP that allows you to control FreeCAD from Claude Code or Claude Desktop.

## Demo

### Design a flange

![demo](./assets/freecad_mcp4.gif)

### Design a toy car

![demo](./assets/make_toycar4.gif)

### Design a part from 2D drawing

#### Input 2D drawing

![input](./assets/b9-1.png)

#### Demo

![demo](./assets/from_2ddrawing.gif)

This is the conversation history.
https://claude.ai/share/7b48fd60-68ba-46fb-bb21-2fbb17399b48

## Install addon

FreeCAD Addon directory is
* Windows: `%APPDATA%\FreeCAD\Mod\`
* Mac:
  * FreeCAD 1.1: `~/Library/Application\ Support/FreeCAD/v1-1/Mod/`
  * FreeCAD 1.0: `~/Library/Application\ Support/FreeCAD/v1-0/Mod/`
* Linux:
  * Ubuntu: `~/.FreeCAD/Mod/` or `~/snap/freecad/common/Mod/` (if you install FreeCAD from snap)
  * Debian: `~/.local/share/FreeCAD/Mod`
  * Arch / CachyOS (FreeCAD 1.1 from `extra/freecad`): `~/.local/share/FreeCAD/v1-1/Mod/`

Please put `addon/FreeCADMCP` directory to the addon directory.

```bash
git clone https://github.com/neka-nat/freecad-mcp.git
cd freecad-mcp

# For Linux (Ubuntu/Debian)
cp -r addon/FreeCADMCP ~/.FreeCAD/Mod/

# For Linux (Arch/CachyOS, FreeCAD 1.1 from extra/freecad)
mkdir -p ~/.local/share/FreeCAD/v1-1/Mod/
cp -r addon/FreeCADMCP ~/.local/share/FreeCAD/v1-1/Mod/

# For macOS (FreeCAD 1.1)
cp -r addon/FreeCADMCP ~/Library/Application\ Support/FreeCAD/v1-1/Mod/
```

When you install addon, you need to restart FreeCAD.
You can select "MCP Addon" from Workbench list and use it.

![workbench_list](./assets/workbench_list.png)

And you can start RPC server by "Start RPC Server" command in "FreeCAD MCP" toolbar.

![start_rpc_server](./assets/start_rpc_server.png)

### Auto-Start RPC Server

By default, the RPC server must be started manually each time FreeCAD opens. To start it automatically:

1. Open the **FreeCAD MCP** menu (switch to the MCP Addon workbench first)
2. Check **Auto-Start Server**

The setting is saved to `freecad_mcp_settings.json` and persists across sessions. On the next FreeCAD launch, the RPC server will start automatically once the application finishes loading.

You can disable it at any time by unchecking **Auto-Start Server** in the same menu.

## Setting up Claude Code

Pre-installation of [uv/uvx](https://docs.astral.sh/uv/guides/tools/) is required.

Register the server once, from anywhere:

```bash
claude mcp add freecad -s user -- uvx freecad-mcp
```

`-s user` makes it available in every project. Use `-s project` instead to write
the config to a `.mcp.json` that you commit and share with a team, or omit `-s`
for the current project only.

To save tokens by skipping the screenshots that most tools return, append the
flag after the command:

```bash
claude mcp add freecad -s user -- uvx freecad-mcp --only-text-feedback
```

For developers working from a clone, this repository already ships a project
`.mcp.json` that runs the local checkout — just start Claude Code from the
repository root and approve the server when prompted:

```bash
git clone https://github.com/neka-nat/freecad-mcp.git
cd freecad-mcp
claude
```

Verify the connection with `/mcp` inside Claude Code, or from the shell:

```bash
claude mcp list
```

Start FreeCAD with the addon's RPC server running before you use the tools (see
[Install addon](#install-addon)), then just describe what you want to build:

```
> model a 80x40x10 mm mounting bracket with four M4 clearance holes
```

Claude Code picks up the bundled `freecad-cad-design` skill from
[`.claude/skills/`](.claude/skills/freecad-cad-design/SKILL.md) when you work in
this repository, which teaches it the parametric modeling workflow, the
`create_object` property format, and the `execute_code` rules. To use it in your
own projects, copy that directory into `~/.claude/skills/`:

```bash
cp -r .claude/skills/freecad-cad-design ~/.claude/skills/
```

The server's `asset_creation_strategy` prompt is also available as the slash
command `/mcp__freecad__asset_creation_strategy`.

## Setting up Claude Desktop

Pre-installation of the [uvx](https://docs.astral.sh/uv/guides/tools/) is required.

And you need to edit Claude Desktop config file, `claude_desktop_config.json`.

For user.

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "freecad-mcp"
      ]
    }
  }
}
```

If you want to save token, you can set `only_text_feedback` to `true` and use only text feedback.

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "freecad-mcp",
        "--only-text-feedback"
      ]
    }
  }
}
```


For developer.
First, you need clone this repository.

```bash
git clone https://github.com/neka-nat/freecad-mcp.git
```

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/freecad-mcp/",
        "run",
        "freecad-mcp"
      ]
    }
  }
}
```

## Remote Connections

By default the RPC server does not accept remote connections and listens on `localhost`. To control FreeCAD from another machine on your network:

### 1. Enable remote connections in FreeCAD

In the **FreeCAD MCP** toolbar:

1. Check **Remote Connections** — the RPC server will bind to `0.0.0.0` (all interfaces) on the next restart. For security reasons, it only accepts connections from the IP addresses or CIDR subnets specified in the **Allowed IPs** field. By default this is `127.0.0.1`.
2. Click **Configure Allowed IPs** and enter a comma-separated list of IP addresses or CIDR subnets that are allowed to connect, e.g.:

   ```
   192.168.1.100, 10.0.0.0/24
   ```

   `127.0.0.1` is always the default. Invalid entries are rejected with an error dialog. Restart the RPC server after changing these settings.

### 2. Point the MCP server at the remote host

Pass the `--host` flag with the IP address or hostname of the machine running FreeCAD:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": [
        "freecad-mcp",
        "--host", "192.168.1.100"
      ]
    }
  }
}
```

In Claude Code, pass the same flag after the command:

```bash
claude mcp add freecad -s user -- uvx freecad-mcp --host 192.168.1.100
```

The `--host` value is validated on startup — it must be a valid IPv4/IPv6 address or hostname.

## Tools

* `create_document`: Create a new document in FreeCAD.
* `create_object`: Create a new object in FreeCAD.
* `edit_object`: Edit an object in FreeCAD.
* `delete_object`: Delete an object in FreeCAD.
* `execute_code`: Execute arbitrary Python code in FreeCAD on the GUI thread.
* `execute_code_async`: Execute background-safe Python code without waiting for completion, for long-running computations that do not touch the GUI or the document tree.
* `insert_part_from_library`: Insert a part from the [parts library](https://github.com/FreeCAD/FreeCAD-library).
* `get_view`: Get a screenshot of the active view.
* `get_objects`: Get all objects in a document.
* `get_object`: Get an object in a document.
* `get_parts_list`: Get the list of parts in the [parts library](https://github.com/FreeCAD/FreeCAD-library).
* `list_documents`: Get the list of open documents in FreeCAD.
* `reload_document`: Close and re-open a document to pick up changes made to its `.FCStd` file outside the GUI.
* `run_fem_analysis`: Run the CalculiX solver on an existing `Fem::FemAnalysis` and return summary results (max von Mises stress, max displacement, node count, working directory). Auto-creates a `SolverCcxTools` if the analysis has none. See [`examples/cantilever_fem.py`](examples/cantilever_fem.py) for an end-to-end usage example.

## YouTube downloader

This repository also ships a standalone `youtube-downloader` command for
downloading YouTube videos, extracting audio, and reading video metadata. It is
independent of the FreeCAD MCP server. See
[`docs/youtube-downloader.md`](docs/youtube-downloader.md).

```bash
uv run youtube-downloader download "https://youtu.be/VIDEO_ID" --quality 1080p
uv run youtube-downloader audio "https://youtu.be/VIDEO_ID" --audio-format mp3
```

## Contributors

<a href="https://github.com/neka-nat/freecad-mcp/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=neka-nat/freecad-mcp" />
</a>

Made with [contrib.rocks](https://contrib.rocks).
