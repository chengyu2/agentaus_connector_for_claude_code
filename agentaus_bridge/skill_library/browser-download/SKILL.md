---
name: browser-download
description: On macOS, download a file, or navigate to a page, that sits behind a login the user already has in their browser - a private Google Sheet, Doc, Slides deck or Drive file, an intranet page, a portal export. Use when a link the user gave returns 401/403 or a sign-in page to curl, or a connector for that service is not signed in. Drives Safari with AppleScript, and watches for and handles the prompts that browsers and websites pop up (download permission, account chooser, cookie banners).
license: Apache-2.0
metadata:
  adapted-for: agentaus
---

# Download or navigate through the user's logged-in browser

## When to use

- The user gave a link to their own file, and `curl` gets `401`, `403` or a sign-in page.
- A connector for the service (Google Sheets, Drive, ...) is not signed in.
- Do not use it to reach anything the user did not point you at.

## Rules

- **Expect a prompt after every step.** A download, a navigation or a click can each raise
  one, from the browser or from the website. Look for it before you wait for a result.
- **Answer a prompt only to let the user's own request through, once.** Click `Allow`, never
  `Always Allow` or a setting that changes the browser for later.
- **Never click** a prompt that saves a password, signs in, grants camera, microphone or
  location access, installs anything, or confirms a payment. Stop and ask the user.
- **A prompt you do not recognise:** do not guess. Take a screenshot, describe it to the
  user, and ask.
- **A sign-in page means the user is not logged in there.** Stop and tell them.

## Steps

### 1. Build a direct link to the file

A direct link downloads the file instead of opening an editor page, so there is no page to
navigate.

| What | Direct link |
| --- | --- |
| Google Sheet | `https://docs.google.com/spreadsheets/d/<ID>/export?format=xlsx` (keeps cell colours; `csv` loses them) |
| Google Doc | `https://docs.google.com/document/d/<ID>/export?format=docx` (or `pdf`, `txt`) |
| Google Slides | `https://docs.google.com/presentation/d/<ID>/export/pptx` (or `/export/pdf`) |
| Google Drive file | `https://drive.google.com/uc?export=download&id=<ID>` |
| Anything else | The site's own download or export link. Without one, navigate (step 6). |

`<ID>` is the long token after `/d/` in the link the user gave.

### 2. Note what is already in the download folder

```bash
ls -t ~/Downloads | head -5
```

You need this to tell the new file from an old one with the same name.

### 3. Open the link in Safari

```bash
open -a Safari "<direct link>"
```

### 4. Look for prompts, and answer the expected one

List every button Safari is showing, in any window, sheet or popover:

```applescript
tell application "System Events" to tell process "Safari"
	set names to {}
	repeat with w in windows
		repeat with el in (entire contents of w)
			try
				if role of el is "AXButton" and name of el is not missing value then set end of names to name of el
			end try
		end repeat
	end repeat
	return names
end tell
```

Then decide:

| You see | It is | Do |
| --- | --- | --- |
| `Allow` with `Cancel` ("Do you want to allow downloads on ...") | Safari's download permission | Click `Allow` (below) |
| An account list ("Choose an account") | Google's account chooser | Pick the account the user named. If they named none, ask. |
| `Accept`, `Reject`, `Got it` near the page bottom | A cookie banner | It does not block a download. When navigating, prefer `Reject` or the smallest choice. |
| `Open in app`, `Not now`, `Install` | An app or extension prompt | Click `Not now` / `Cancel` |
| `Sign in`, a password field | Not logged in | Stop and tell the user |
| Anything else | Unknown | Screenshot (`screencapture -x /tmp/prompt.png`), read it, ask the user |

Click a named button, waiting up to 15 seconds for it to appear:

```applescript
tell application "System Events" to tell process "Safari"
	set frontmost to true
	repeat 15 times
		repeat with w in windows
			repeat with el in (entire contents of w)
				try
					if role of el is "AXButton" and name of el is "Allow" then
						click el
						return "clicked"
					end if
				end try
			end repeat
		end repeat
		delay 1
	end repeat
	return "not found"
end tell
```

**Example:** the first Google Sheet download of a session shows "Do you want to allow downloads
on docs.google.com?". Nothing downloads until `Allow` is clicked. Without this step the file
never arrives, and the wait in step 5 times out.

System Events needs Accessibility permission for the terminal or editor running you. If the
click fails with "not allowed assistive access", tell the user where to grant it
(System Settings > Privacy & Security > Accessibility).

### 5. Wait for the file, then check it is the file

```bash
for i in $(seq 1 30); do
  f=$(ls -t ~/Downloads | grep -v '\.download$' | head -1)
  [ -n "$f" ] && [ $(( $(date +%s) - $(stat -f %m ~/Downloads/"$f") )) -lt 120 ] && break
  sleep 2
done
file ~/Downloads/"$f"
```

- A `.download` file is still arriving. Wait for it to finish.
- `file` must name the real type ("Microsoft Excel 2007+", "PDF document"). **"HTML document"
  is an error or sign-in page saved under the file's name.** Open it, read why, and go back
  to step 4.
- Still nothing after 60 seconds: go back to step 4. A prompt is almost always the reason.

### 6. When you must navigate instead

- After every navigation or click, repeat step 4 before acting on the page.
- Read the page with `osascript -e 'tell application "Safari" to do JavaScript "document.title" in current tab of front window'`.
  This needs Safari's Develop > Allow JavaScript from Apple Events. If it is off, ask the user
  to turn it on: it is a browser setting, so do not change it yourself.

## Reading what you downloaded

Read it with `Read` or `agentaus_zoom`: both read `.xlsx`, `.docx` and `.pdf`. For a
spreadsheet whose meaning is in its colours, read the fills with `openpyxl`
(`cell.fill.fgColor.rgb`, for example `FF00FF00` green, `FFFFFF00` yellow).
