// Product discovery metadata. Audit sources and parity proof live in private coverage notes.
export const NATIVE_SURFACE_ACTIONS = [
  {
    "id": "session.list",
    "name": "sessions",
    "aliases": [
      "resume",
      "continue"
    ],
    "help": "List chats and resume a chat",
    "status": "host-equivalent",
    "destination": "/sessions",
    "reason": "",
    "catalogToken": "/sessions"
  },
  {
    "id": "session.new",
    "name": "new",
    "aliases": [
      "clear"
    ],
    "help": "Start a new chat",
    "status": "host-equivalent",
    "destination": "/new",
    "reason": "",
    "catalogToken": "/new"
  },
  {
    "id": "workflow.list",
    "name": "workflows",
    "aliases": [],
    "help": "Browse workflows and runs",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.recover",
    "name": "recover",
    "aliases": [],
    "help": "List interrupted candidates or explicitly recover one",
    "status": "implemented",
    "destination": "/recover",
    "reason": "",
    "catalogToken": "/recover"
  },
  {
    "id": "session.rename",
    "name": "rename",
    "aliases": [],
    "help": "Rename the current chat",
    "status": "host-equivalent",
    "destination": "/rename",
    "reason": "",
    "catalogToken": "/rename"
  },
  {
    "id": "session.timeline",
    "name": "timeline",
    "aliases": [],
    "help": "List native undo boundaries",
    "status": "implemented",
    "destination": "/timeline",
    "reason": "",
    "catalogToken": "/timeline"
  },
  {
    "id": "session.fork",
    "name": "fork",
    "aliases": [],
    "help": "Fork a chat at a message boundary",
    "status": "implemented",
    "destination": "/native-fork",
    "reason": "",
    "catalogToken": "/native-fork"
  },
  {
    "id": "session.compact",
    "name": "compact",
    "aliases": [
      "summarize"
    ],
    "help": "Compact the active native chat context",
    "status": "implemented",
    "destination": "/compact",
    "reason": "",
    "catalogToken": "/compact"
  },
  {
    "id": "session.ask",
    "name": "btw",
    "aliases": [],
    "help": "Ask a read-only side question",
    "status": "implemented",
    "destination": "/btw",
    "reason": "",
    "catalogToken": "/btw"
  },
  {
    "id": "session.undo",
    "name": "undo",
    "aliases": [],
    "help": "Revert native history at a user-turn boundary",
    "status": "implemented",
    "destination": "/undo",
    "reason": "",
    "catalogToken": "/undo"
  },
  {
    "id": "session.redo",
    "name": "redo",
    "aliases": [],
    "help": "Restore a reversible native history boundary",
    "status": "implemented",
    "destination": "/redo",
    "reason": "",
    "catalogToken": "/redo"
  },
  {
    "id": "session.share",
    "name": "share",
    "aliases": [],
    "help": "Publish a chat through its sharing owner",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.unshare",
    "name": "unshare",
    "aliases": [],
    "help": "Revoke a published chat",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.copy",
    "name": "copy",
    "aliases": [],
    "help": "Copy loaded conversation text using message Copy format",
    "status": "host-equivalent",
    "destination": "/copy",
    "reason": "",
    "catalogToken": "/copy"
  },
  {
    "id": "session.export",
    "name": "export",
    "aliases": [],
    "help": "Export a transcript",
    "status": "host-equivalent",
    "destination": "/export",
    "reason": "",
    "catalogToken": "/export"
  },
  {
    "id": "worktree.list",
    "name": "worktree",
    "aliases": [
      "wt"
    ],
    "help": "Manage Git worktrees",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.skills",
    "name": "skills",
    "aliases": [],
    "help": "Find eligible skills",
    "status": "host-equivalent",
    "destination": "/skills",
    "reason": "",
    "catalogToken": "/skills"
  },
  {
    "id": "prompt.editor",
    "name": "editor",
    "aliases": [],
    "help": "Edit a draft in the native terminal external editor",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.list",
    "name": "models",
    "aliases": [],
    "help": "Find available models",
    "status": "host-equivalent",
    "destination": "/models",
    "reason": "",
    "catalogToken": "/models"
  },
  {
    "id": "agent.list",
    "name": "agents",
    "aliases": [],
    "help": "List or select an eligible profile for the next Agent turn",
    "status": "implemented",
    "destination": "/agent-profile",
    "reason": "",
    "catalogToken": "/agent-profile"
  },
  {
    "id": "agent.force",
    "name": "force-agent",
    "aliases": [],
    "help": "Force a native agent switch",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.modalities",
    "name": "modalities",
    "aliases": [],
    "help": "Configure model input modalities",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.context_limit",
    "name": "context-limit",
    "aliases": [],
    "help": "Configure a model context limit",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "variant.list",
    "name": "variants",
    "aliases": [],
    "help": "Choose a model variant",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "question.never_ask.toggle",
    "name": "never-ask",
    "aliases": [],
    "help": "Configure automatic question decisions",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "permission.skip_all.toggle",
    "name": "skip-permissions",
    "aliases": [],
    "help": "Configure native permission skipping",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "mcp.list",
    "name": "mcps",
    "aliases": [],
    "help": "Inspect MCP connections",
    "status": "host-equivalent",
    "destination": "/mcp",
    "reason": "",
    "catalogToken": "/mcp"
  },
  {
    "id": "provider.login",
    "name": "login",
    "aliases": [],
    "help": "Open provider account settings; choose MiMo to sign in",
    "status": "host-equivalent",
    "destination": "Settings provider account owner",
    "reason": "",
    "settingsDestination": {
      "panel": "services",
      "field": "mimo-provider-directory"
    }
  },
  {
    "id": "provider.connect",
    "name": "connect",
    "aliases": [],
    "help": "Open provider account settings; choose MiMo to connect",
    "status": "host-equivalent",
    "destination": "Settings provider account owner",
    "reason": "",
    "settingsDestination": {
      "panel": "services",
      "field": "mimo-provider-directory"
    }
  },
  {
    "id": "provider.logout",
    "name": "logout",
    "aliases": [],
    "help": "Open connected provider accounts; choose the MiMo connection to disconnect",
    "status": "host-equivalent",
    "destination": "Settings provider account owner",
    "reason": "",
    "settingsDestination": {
      "panel": "added-models",
      "field": "mimo-connected-provider-list"
    }
  },
  {
    "id": "console.org.switch",
    "name": "org",
    "aliases": [
      "orgs",
      "switch-org"
    ],
    "help": "Choose a MiMo organization",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "consent.revoke",
    "name": "revoke-consent",
    "aliases": [],
    "help": "Revoke MiMo service consent",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "opencode.status",
    "name": "status",
    "aliases": [],
    "help": "Inspect native engine status",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "theme.switch",
    "name": "themes",
    "aliases": [],
    "help": "Open Appearance Settings",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "background.switch",
    "name": "background",
    "aliases": [],
    "help": "Choose a terminal image background",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "logo.switch",
    "name": "logo",
    "aliases": [],
    "help": "Choose native terminal branding",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "theme.switch_mode.dark",
    "name": "dark",
    "aliases": [],
    "help": "Select a dark appearance",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "theme.switch_mode.light",
    "name": "light",
    "aliases": [],
    "help": "Select a light appearance",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "language.switch",
    "name": "language",
    "aliases": [
      "lang"
    ],
    "help": "Open language preferences",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.toggle.timestamps",
    "name": "timestamps",
    "aliases": [
      "toggle-timestamps"
    ],
    "help": "Open browser timestamp display settings",
    "status": "host-equivalent",
    "destination": "appearance Settings",
    "reason": "",
    "settingsDestination": {
      "panel": "appearance",
      "field": "show-timestamps"
    }
  },
  {
    "id": "session.toggle.thinking",
    "name": "thinking",
    "aliases": [
      "toggle-thinking"
    ],
    "help": "Open browser reasoning display settings",
    "status": "host-equivalent",
    "destination": "appearance Settings",
    "reason": "",
    "settingsDestination": {
      "panel": "appearance",
      "field": "show-thinking"
    }
  },
  {
    "id": "voice.toggle",
    "name": "voice",
    "aliases": [],
    "help": "Configure voice capture",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "voice.send",
    "name": "voice-send",
    "aliases": [],
    "help": "Configure spoken send commands",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "voice.control",
    "name": "voice-control",
    "aliases": [],
    "help": "Configure spoken control commands",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "help.show",
    "name": "help",
    "aliases": [],
    "help": "Search available commands",
    "status": "host-equivalent",
    "destination": "/commands",
    "reason": "",
    "catalogToken": "/commands"
  },
  {
    "id": "docs.open",
    "name": "doc",
    "aliases": [
      "docs"
    ],
    "help": "Open MiMo documentation",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "app.exit",
    "name": "exit",
    "aliases": [
      "quit",
      "q"
    ],
    "help": "Exit the native terminal application",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "model.cycle_recent",
    "name": "",
    "aliases": [],
    "help": "Native model cycle_recent control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.cycle_recent_reverse",
    "name": "",
    "aliases": [],
    "help": "Native model cycle_recent_reverse control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.cycle_favorite",
    "name": "",
    "aliases": [],
    "help": "Native model cycle_favorite control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "model.cycle_favorite_reverse",
    "name": "",
    "aliases": [],
    "help": "Native model cycle_favorite_reverse control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "agent.cycle",
    "name": "",
    "aliases": [],
    "help": "Native agent cycle control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "agent.cycle.reverse",
    "name": "",
    "aliases": [],
    "help": "Native agent cycle reverse control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "variant.cycle",
    "name": "",
    "aliases": [],
    "help": "Native variant cycle control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "theme.mode.lock",
    "name": "",
    "aliases": [],
    "help": "Native theme mode lock control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "app.debug",
    "name": "",
    "aliases": [],
    "help": "Native app debug control",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "app.console",
    "name": "",
    "aliases": [],
    "help": "Native app console control",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "app.heap_snapshot",
    "name": "",
    "aliases": [],
    "help": "Native app heap_snapshot control",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "terminal.suspend",
    "name": "",
    "aliases": [],
    "help": "Native terminal suspend control",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "terminal.title.toggle",
    "name": "",
    "aliases": [],
    "help": "Native terminal title toggle control",
    "status": "terminal-only",
    "destination": "native terminal",
    "reason": "This action manages the native terminal application."
  },
  {
    "id": "app.toggle.animations",
    "name": "",
    "aliases": [],
    "help": "Native app toggle animations control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "app.toggle.diffwrap",
    "name": "",
    "aliases": [],
    "help": "Open browser diff wrapping settings",
    "status": "host-equivalent",
    "destination": "appearance Settings",
    "reason": "",
    "settingsDestination": {
      "panel": "appearance",
      "field": "wrap-diffs"
    }
  },
  {
    "id": "session.sidebar.toggle",
    "name": "",
    "aliases": [],
    "help": "Native session sidebar toggle control",
    "status": "host-equivalent",
    "destination": "/sidebar",
    "reason": "",
    "catalogToken": "/sidebar"
  },
  {
    "id": "session.toggle.conceal",
    "name": "",
    "aliases": [],
    "help": "Native session toggle conceal control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.toggle.actions",
    "name": "",
    "aliases": [],
    "help": "Open browser tool detail display settings",
    "status": "host-equivalent",
    "destination": "appearance Settings",
    "reason": "",
    "settingsDestination": {
      "panel": "appearance",
      "field": "show-tool-details"
    }
  },
  {
    "id": "session.toggle.scrollbar",
    "name": "",
    "aliases": [],
    "help": "Native session toggle scrollbar control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.toggle.generic_tool_output",
    "name": "",
    "aliases": [],
    "help": "Open browser tool output display settings",
    "status": "host-equivalent",
    "destination": "appearance Settings",
    "reason": "",
    "settingsDestination": {
      "panel": "appearance",
      "field": "show-tool-output"
    }
  },
  {
    "id": "session.page.up",
    "name": "",
    "aliases": [],
    "help": "Native session page up control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.page.down",
    "name": "",
    "aliases": [],
    "help": "Native session page down control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.line.up",
    "name": "",
    "aliases": [],
    "help": "Native session line up control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.line.down",
    "name": "",
    "aliases": [],
    "help": "Native session line down control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.half.page.up",
    "name": "",
    "aliases": [],
    "help": "Native session half page up control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.half.page.down",
    "name": "",
    "aliases": [],
    "help": "Native session half page down control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.first",
    "name": "",
    "aliases": [],
    "help": "Native session first control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.last",
    "name": "",
    "aliases": [],
    "help": "Native session last control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.messages_last_user",
    "name": "",
    "aliases": [],
    "help": "Native session messages_last_user control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.message.next",
    "name": "",
    "aliases": [],
    "help": "Native session message next control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.message.previous",
    "name": "",
    "aliases": [],
    "help": "Native session message previous control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "messages.copy",
    "name": "",
    "aliases": [],
    "help": "Native messages copy control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.child.first",
    "name": "",
    "aliases": [],
    "help": "Native session child first control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.child.next",
    "name": "",
    "aliases": [],
    "help": "Native session child next control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.child.previous",
    "name": "",
    "aliases": [],
    "help": "Native session child previous control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.parent",
    "name": "",
    "aliases": [],
    "help": "Native session parent control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.clear",
    "name": "",
    "aliases": [],
    "help": "Native prompt clear control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.submit",
    "name": "",
    "aliases": [],
    "help": "Native prompt submit control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.paste",
    "name": "",
    "aliases": [],
    "help": "Native prompt paste control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "session.interrupt",
    "name": "",
    "aliases": [],
    "help": "Native session interrupt control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.stash",
    "name": "",
    "aliases": [],
    "help": "Native prompt stash control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.stash.pop",
    "name": "",
    "aliases": [],
    "help": "Native prompt stash pop control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "prompt.stash.list",
    "name": "",
    "aliases": [],
    "help": "Native prompt stash list control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "plugins.list",
    "name": "",
    "aliases": [],
    "help": "Native plugins list control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "plugins.install",
    "name": "",
    "aliases": [],
    "help": "Native plugins install control",
    "status": "host-equivalent",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  },
  {
    "id": "tips.toggle",
    "name": "",
    "aliases": [],
    "help": "Native tips toggle control",
    "status": "incomplete",
    "destination": "native capability",
    "reason": "Not available in Open Clank yet. Use the native terminal."
  }
];
