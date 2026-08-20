# Setup

Run one installer for your operating system and provide the path to an exported
`chatgpt.com` cookie JSON file. The installer owns the private Python environment, installs the
browser runtime, configures credentials, and creates the `swoon` terminal command.

```text
Linux/macOS: ./setup/install.sh /path/to/cookies.json
Windows:     setup\windows\install.cmd -Cookies C:\path\to\cookies.json
```

After opening a new terminal, `swoon` starts the default interactive coding agent and
`swoon NAME` creates or resumes a named workspace. The agent opens Chromium and keeps it alive for
the terminal session so the user can complete a human-verification check. `/quit` closes it. The
development scripts enable the same visible mode with verbose transport logs. Multiline terminal
paste is accepted as one task; `/paste` starts an explicit fallback terminated by `/end`.

If ChatGPT presents a Cloudflare human-verification check, run `swoon auth` once. This explicit
command opens Chromium so the user can refresh the private configured browser state separately,
then closes the window. It does not automate or bypass the verification. `swoon NAME --headless`
is an explicit opt-in for environments where no human check is presented.
