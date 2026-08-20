# Setup

Run one installer for your operating system and provide the path to an exported
`chatgpt.com` cookie JSON file. The installer owns the private Python environment, installs the
browser runtime, configures credentials, and creates the `swoon` terminal command.

```text
Linux/macOS: ./setup/install.sh /path/to/cookies.json
Windows:     setup\windows\install.cmd -Cookies C:\path\to\cookies.json
```

After opening a new terminal, `swoon` starts the default interactive coding agent and
`swoon NAME` creates or resumes a named workspace. Consumer launches are headless. Developers can
use `setup/dev/start-headed.sh NAME` or `setup/dev/start-headed.ps1 NAME` to expose the browser and
transport logs.
