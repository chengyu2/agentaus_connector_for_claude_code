---
name: mac-mail-calendar
description: On macOS, use AppleScript to find and read emails in Apple Mail (body, attachments, zip files of resumes or forms), save draft emails from a chosen account, clean up duplicate drafts, send email with the user's own signature once the user confirms the list, and create Calendar events with invitations. Use when asked to search, read or reply to emails in Apple Mail, draft emails to a list of people, clean up or send them, or schedule a meeting or interview and invite people. Covers the traps that make these look done when they are not - Calendar's AppleScript adds no attendees, and a slow or failed search is not a missing feature.
license: Apache-2.0
metadata:
  adapted-for: agentaus
---

# Apple Mail and Calendar with AppleScript

Placeholders in angle brackets: `<account>` is an account's name as Mail's sidebar shows it,
`<Name> <address>` is the user's own sender identity on that account, and so on. Find the
real values first (section 1); never guess them.

## Rules

- **Never send** email or invitations unless the user explicitly said to send **and**
  confirmed the exact list of what goes out (section 5). Otherwise save drafts.
- **Never delete a draft you did not make.** Clean-up only touches drafts addressed to the
  people you drafted for, and shows the user what it would delete before it deletes.
- **Check every write by reading it back:** count the drafts in that account's `Drafts`,
  and read the fields of the event you just made through the reference `make` returned
  (the recipe in section 6 does this). A command that returns without an error has proved
  nothing. Do not search the calendar afterwards for the event you hold: that search is slow
  and tells you nothing more.
- **A failed command is not a missing feature.** `tell application "Mail" to get properties`
  fails, yet messages can be read. Try the recipes below before concluding anything.
- **Mail does not promise an order** for `messages of ...`. Pick a message by its
  `date received` or `id`, never as the first or last item of that list.
- **Search narrowly:** one account, its mailboxes, a date window, then a sender or subject
  test. Reading every message of every mailbox takes minutes on a real mailbox.
- **One recipient per draft** when writing to several people, so no one sees the others'
  addresses.
- Write longer scripts to a `.applescript` file and run `osascript file.applescript args`.
  Quoting a long script inside `osascript -e '...'` breaks on the first apostrophe.
- **Let Mail add the signature.** End the body with a short sign-off and the user's first
  name (`Thanks,` then `<first name>`), then set `message signature` to the user's Mail
  signature (`tell application "Mail" to name of every signature`). Never type a name, title
  or company block into the body: it duplicates the real signature and has no logo.
- **No empty first line.** Mail puts an empty line above every body a script sets. Remove
  it before saving or sending (section 4a), and check the first line is the greeting.
- **Warn the user before Mail comes to the front.** Removing that line needs the message
  window in front, and anything the user types meanwhile lands in the message. Say how long
  to keep hands off the keyboard, and say when it is done.
- **Test on the user's own address, then clean up.** Throwaway test drafts and windows are
  discarded: answer a Save / Don't Save sheet on them with **Don't Save**, never Save.
- **Read data files with Python, not `while IFS=$'\t' read`.** Bash collapses empty
  tab-separated fields, so the next column (a file name) lands in the Cc field.

## 1. Find the accounts and calendars

```applescript
tell application "Mail"
	set out to ""
	repeat with a in accounts
		set out to out & (name of a) & " = " & ((email addresses of a) as text) & linefeed
	end repeat
	return out
end tell
```

```applescript
tell application "Calendar" to get name of every calendar
```

The user names an address; use the account whose `email addresses` contain it, and that
account's calendar. A Microsoft 365 or Exchange calendar is often named just `Calendar`.

## 2. Find an email

```applescript
on run argv
	set acct to item 1 of argv
	set who to item 2 of argv
	set cutoff to (current date) - (30 * days)
	set out to ""
	tell application "Mail"
		repeat with mb in mailboxes of account acct
			try
				repeat with m in (messages of mb whose date received > cutoff and sender contains who)
					set out to out & (date received of m as string) & " | " & (name of mb) & " | " & (subject of m) & linefeed
				end repeat
			end try
		end repeat
	end tell
	return out
end run
```

Run: `osascript find.applescript "<account>" "<sender address or name>"`. Use
`subject contains "<words>"` in place of the sender test to search by subject. Sent mail is
in a mailbox named `Sent Items` or `Sent Mail`, depending on the account.

## 3. Read it and save its attachments

```applescript
on run argv
	set acct to item 1 of argv
	set who to item 2 of argv
	set dest to item 3 of argv -- an existing, empty folder
	tell application "Mail"
		set ms to (messages of mailbox "Inbox" of account acct whose date received > ((current date) - 30 * days) and sender contains who)
		if (count ms) is 0 then return "NO MESSAGE from " & who
		set m to item 1 of ms
		repeat with x in ms -- the newest, by date: the list has no promised order
			if date received of x > date received of m then set m to contents of x
		end repeat
		repeat with a in mail attachments of m
			save a in POSIX file (dest & "/" & (name of a))
		end repeat
		return content of m
	end tell
end run
```

Then unpack and read:

```bash
unzip -q -o "<file>.zip" -d unzipped/
pdftotext -layout "<document>.pdf" - | head -20
pdftotext "<document>.pdf" - | grep -oE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' | sort -u
```

- A CV can also list its referees' addresses. The person's own address is in the header,
  next to their name. When one document has no address, look in their other one.
- Applications and CVs hold personal information. Keep them in a scratch folder, and print
  names and addresses, not whole documents.

## 4. Save a draft from a chosen account, with an attachment

```applescript
on run argv
	-- argv: account, recipient, subject, body, sender, signature, then optionally the attachment's path
	set acct to item 1 of argv
	set toAddr to item 2 of argv
	set subj to item 3 of argv
	set body to item 4 of argv
	set fromId to item 5 of argv -- exactly "<Name> <address>" of that account
	set sigName to item 6 of argv -- one of: tell application "Mail" to name of every signature
	set wanted to 0
	if (count of argv) > 6 then set wanted to 1
	with timeout of 300 seconds
		tell application "Mail"
			set mb to item 1 of (mailboxes of account acct whose name is "Drafts")
			repeat with x in (messages of mb whose subject is subj)
				repeat with r in to recipients of x
					if address of r is toAddr then return "DRAFT EXISTS for " & toAddr & " - nothing made"
				end repeat
			end repeat
			if (count (windows whose name is subj)) > 0 then return "WINDOW OPEN titled " & subj & " - nothing made"
			-- the body ends with "Thanks," and the first name; Mail adds the signature and its logo
			set m to make new outgoing message with properties {subject:subj, content:body, visible:true, sender:fromId}
			set message signature of m to signature sigName
			tell m
				make new to recipient at end of to recipients with properties {address:toAddr}
				if wanted > 0 then
					tell content to make new attachment with properties {file name:(POSIX file (item 7 of argv))} at after last paragraph
				end if
			end tell
			delay 3 -- let the attachment load, then make sure it did
			set n to 0
			repeat 30 times
				set n to count attachments of content of m
				if n ≥ wanted then exit repeat
				delay 0.5
			end repeat
			if n < wanted then
				close (first window whose name is subj) saving no
				return "ATTACHMENT MISSING for " & toAddr & " - nothing saved"
			end if
			-- section 4a: remove the empty first line here, then read back, before saving
			save m
			delay 2
			close (first window whose name is subj) saving no
			return "DRAFT " & toAddr & " (" & n & " attachment)"
		end tell
	end timeout
end run
```

Run: `osascript draft.applescript "<account>" "<to>" "<subject>" "<body>" "<Name> <address>" "<signature>" /path/invite.ics`.
It prints `DRAFT <address>` when one complete draft was saved. Any other line means nothing
was saved for that person:

- `DRAFT EXISTS` - that person already has a draft with this subject. Running the recipe
  again never makes a second copy. To replace a draft (corrected text or `.ics`), ask the
  user, delete the old one, then run it again.
- `WINDOW OPEN` - a window with this subject is already open, perhaps one the user is
  writing. The script closes its own window by its subject, so it will not start while
  another one could be closed by mistake. Ask the user to close or save it.
- `ATTACHMENT MISSING` - the attachment did not load within 18 seconds. Its window was
  closed unsaved. Check the file path and run it again.

Why the recipe is built this way:

- **Make the draft visible, save it once, and close its window.** Measured:
  - `visible:false` drafts never close: `close m` and `delete m` leave them open, they pile up
    (`tell application "Mail" to count outgoing messages` keeps growing), and at about 20 Mail
    stopped answering AppleScript at all until it was quit and reopened.
  - `save m` followed by `close m saving yes`, run for 17 people back to back, left 29 drafts:
    extra copies, some with no attachment and three with no recipient.
  - The recipe above left exactly one complete draft per run and no open window. Its window
    shows on screen for a few seconds. That is why it closes the window by subject rather
    than with `close m`, and why it refuses to start when such a window is already open.
- Check `count outgoing messages` before and after: it must not grow.
- Run one script against Mail at a time. Mail answers one AppleScript request at a time: a
  second script waits behind a long one and fails after two minutes with "AppleEvent timed out".
- If Mail stops answering even `count outgoing messages`, scripts cannot fix it. Ask the user
  before quitting Mail: open drafts they have not saved would be lost.
- The draft lands in the `Drafts` mailbox of the account that `sender` belongs to. A
  `sender` that matches no account puts it in the default account.

### 4a. Remove the empty first line Mail adds

Whatever order you set the body and the signature in, Mail starts the HTML with
`<div class="Apple-Mail-URLShareUserContentTopClass"><br></div>`: the reader sees an empty
line above "Hi <name>,". AppleScript cannot reach that HTML. Measured:

- Setting the body before or after the signature, or after the window opened: the line stays.
- `visible:false` (no window): the line stays, the signature's logo is dropped, and every line
  of the plain-text part starts with `>`. Do not use it for real email.
- Rebuilding a saved draft as a new message (`scripts/send-draft.applescript`) copies the
  line and drops the logo.
- What works: with the message window in front, Cmd-Up then Forward Delete, once.

That needs Mail in front for a few seconds per message, and keys typed by the user in that
time go into the message. On 26 Nov 2026 a test message had its recipient replaced by the
user's typing and went to someone else, and a stray "s" reached another email. So:

1. **Before the batch, tell the user** how long Mail will come forward and to keep hands
   off the keyboard until you say it is done. If they cannot, offer drafts they finish
   themselves.
2. Find the body by walking the window's tree: window > group > group > scroll area >
   `AXWebArea`. `entire contents` of a Mail compose window returns nothing.
3. Press the keys only while `name of (first process whose frontmost is true)` is "Mail".
4. Read back before saving or sending: paragraph 1 of `content` starts with the greeting,
   To, Cc and Bcc are exactly what you set, and the subject is unchanged. Otherwise discard.

`scripts/send-message.applescript` (section 5) does all four; copy its `findWebArea` and
the block under "remove the empty first line" for drafts.

### Check: exactly one complete draft per person

Count the drafts with that subject: it should equal the number of people. If it is more, or
some are incomplete, run the clean-up below. It only looks at drafts whose recipients are
all on the list you pass in (or that have none, as broken copies do). Anything else, such as
the user's own draft to an interview panel under the same subject, is reported `UNTOUCHED`.

For each person it keeps one draft: the newest with all its attachments, or, if none has
them, the newest one, flagged for the user to check. It never deletes a person's only draft.
"Newest" is the highest message `id`, because Mail does not promise any order for
`messages of`: never pick by position in that list.

The script is `scripts/dedupe-drafts.applescript`, next to this file. It reads the drafts
once, decides, reports, and only with `delete` deletes - by message `id` - and then reads
the mailbox again. Each line of its report is `KEEP`, `NONE` (no draft for that person),
`DELETE` or `UNTOUCHED`, with the draft's id, recipients and attachment count.

1. Run `osascript <skill-dir>/scripts/dedupe-drafts.applescript "<account>" "<subject>" 1 "a@x.com,b@y.com" list`
   (`1` attachment each, `0` when the drafts carry none).
2. Show the user the `DELETE` lines, and any `MISSING ATTACHMENT` or `NONE` line. Run it
   with `delete` only after they agree.
3. Run it with `list` once more: it should show one `KEEP` per person and no `DELETE`.

## 5. Send - only after the user confirms the list

Build each message fresh and send it with `scripts/send-message.applescript`, next to this
file. It sets the user's Mail signature (with its logo), removes the empty first line
(section 4a), reads back the first line, To, Cc, Bcc and subject, sends, and treats a Mail
warning sheet ("does not appear to be a valid email address") as not sent. A message it
refuses is discarded, not saved.

Do not send by rebuilding a saved draft. AppleScript cannot send a draft, and
`scripts/send-draft.applescript`, which rebuilds one, sends plain text with the empty first
line and without the signature's logo. Use it only if the user asks for drafts they edited
themselves to go out as they are, and has been told how they will look.

1. Write one body file per person: the greeting first, ending with `Thanks,` and the user's
   first name, with no signature block. Drive the batch from Python, not a bash `read` loop.
2. Show the user the whole list: each recipient with their Cc, the sending account, the
   subject, the attachment names and the start of the text.
3. Send only after the user confirms **that list**. A "send them" given before they saw it,
   or before a re-draft, is not a confirmation: show the list and ask.
4. **Test first:** send one to the user's own address and read its source (`source of m`):
   the HTML body must not start with an empty `<div>`, and it must contain an `<img`. Then
   delete the test from the Inbox and the Sent mailbox.
5. **Warn the user** (section 4a): Mail comes to the front for about 10 seconds per message.
   Ask them to keep hands off the keyboard until you say the batch is done.
6. Send one person at a time, and read each result before the next:

   ```bash
   osascript <skill-dir>/scripts/send-message.applescript "<account>" "<subject>" "<to>" "<cc or empty>" \
       /path/body.txt /path/invite.ics "<signature name>" "<Sent mailbox>" nowait
   ```

   The Sent mailbox's name is the one section 2 found, such as `Sent Items` or `Sent Mail`.
   Pass `""` for no Cc or no attachment. Without `nowait`, the script also waits up to 60
   seconds for the message to show in the Sent mailbox, holding Mail in front meanwhile.

   | Result | What it means | Do |
   | --- | --- | --- |
   | `SENT <address>` | Mail sent it. Without `nowait`, it is also in the Sent mailbox. | Next person. |
   | `NOT SENT <address> - ...` | Discarded before sending. Nothing went out. | Stop the batch. Fix the cause it names, then run it again for that person. |
   | `UNCONFIRMED`, an error, "AppleEvent timed out", or anything else | Unknown: Mail may still be sending it. | Stop. Look in the Sent mailbox and the Outbox for that subject and recipient. Never send to that person again until both show nothing for them. |

7. **Look for a prompt the script did not answer:** list Mail's windows and their sheets
   (`tell application "System Events" to tell process "Mail" to get count sheets of every
   window`). On a window the user opened, stop and ask rather than clicking it.
   This call needs Accessibility access for the app running `osascript`, and so does the
   script. If it errors (-1719, -25211) or a permission dialog appears, an error is **not**
   "no prompt": stop sending, tell the user to allow it in System Settings > Privacy &
   Security > Accessibility (for the terminal or the bridge), and ask them to look at Mail's
   windows.
8. Check at the end by listing again, not from memory: the Sent mailbox has one message with
   that subject for each person sent, with the right To and Cc, the attachment, the logo
   (`content` contains `￼`), and the greeting as the first non-empty paragraph. Sent mail
   can take over a minute to file. Then tell the user the batch is done.
9. If you saved drafts for these people earlier, delete only those (section 4's clean-up
   lists them), and only once each person's message is in the Sent mailbox.

Notes:

- `files` is a reserved word inside `tell application "Mail"`: name a list of paths
  `fileList`, or the script stops with "Can't set every file".
- Mail's dictionary declares that `send` returns `false` when it fails, with no error; and an
  error after `send` can come while the message is going out. That is why only the Sent
  mailbox decides that a message was sent.
- `close ... saving no` on a compose window can still raise a "Save / Don't Save / Cancel"
  sheet. On your own failed or test message, click **Don't Save**.

## 6. Create the event, and invite people

### The trap

`make new attendee` on a Calendar event **returns no error and adds no one**. Measured on
macOS: afterwards `count attendees of ev` is still `0`. The event exists, nobody is invited,
and nothing says so.

### What works

1. Create the event on the user's own calendar (below).
2. Invite each person with an `.ics` invitation (`METHOD:REQUEST`) attached to their draft
   (section 4). Opening it puts the event in their calendar and lets them reply.

```applescript
on run argv
	-- argv: calendar, title, location, year, month number, day, hour, minute, minutes long
	set d to current date
	set day of d to 1
	set year of d to (item 4 of argv) as integer
	set month of d to (item 5 of argv) as integer
	set day of d to (item 6 of argv) as integer
	set hours of d to (item 7 of argv) as integer
	set minutes of d to (item 8 of argv) as integer
	set seconds of d to 0
	tell application "Calendar"
		set ev to make new event at end of events of calendar (item 1 of argv) with properties {summary:(item 2 of argv), location:(item 3 of argv), start date:d, end date:d + ((item 9 of argv) as integer) * minutes}
		return (start date of ev as string) & " | " & (location of ev)
	end tell
end run
```

- Build dates by setting each part, as above (`day` to 1 first, so moving to a shorter
  month cannot overflow). A date string such as `date "27/11/2026"` depends on the Mac's
  region settings.
- Search a calendar only **before** creating an event, when an earlier attempt may already
  have made it - to avoid a duplicate. Read events of one calendar for one day:
  `events of calendar "<name>" whose start date ≥ d0 and start date < d0 + 1 * days`.
  Even that can take minutes on a large Exchange or Google calendar, past AppleScript's
  2-minute limit ("AppleEvent timed out"). Wrap it, and any search of a big mailbox, in
  `with timeout of 600 seconds` ... `end timeout`. A timeout is not a missing event:
  rerun the check with the longer limit.

The invitation, one file per person:

```python
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def invitation(name, email, title, location, start_local, minutes, tz, organizer_name, organizer_email):
    start = start_local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))
    end = start + timedelta(minutes=minutes)
    fmt = "%Y%m%dT%H%M%SZ"
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mac-mail-calendar//EN", "METHOD:REQUEST",
        "BEGIN:VEVENT", f"UID:{start.strftime(fmt)}-{email}",
        f"DTSTAMP:{datetime.now(ZoneInfo('UTC')).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}",
        f"SUMMARY:{title}", f"LOCATION:{location}",
        f"ORGANIZER;CN={organizer_name}:mailto:{organizer_email}",
        f"ATTENDEE;CN={name};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}",
        "END:VEVENT", "END:VCALENDAR", ""])

# Example: 3:30 pm on 27 November 2026, Canberra time, for 30 minutes.
text = invitation("Ada Lovelace", "ada@example.com", "Interview", "Level 1, 1 Example St",
                  datetime(2026, 11, 27, 15, 30), 30, "Australia/Sydney", "Sam Organiser", "sam@example.com")
open("invite-ada.ics", "w", newline="").write(text)
```

The time zone is the user's (`readlink /etc/localtime` names it), not UTC.

## 7. Dates given without a month or year

"Friday the 27th" is the next date that is both a Friday and the 27th. Check before using it:
`python3 -c "import datetime; print(datetime.date(2026, 11, 27).strftime('%A'))"`. If that is
not the weekday the user named, ask rather than guess. A length the user did not give (an
interview's) is an assumption: say which one you made.

## 8. Finish by reporting what exists

Read back and report: how many drafts are in which account's `Drafts`, and to whom; the
event's calendar, start and location; and what was sent:

- Section 5 did not run: say nothing was sent, so the invitations go out when the user
  sends the drafts.
- Section 5 ran: who was sent (`SENT`, and now in the Sent mailbox), who was not, and any
  whose outcome is still unknown, with what the user should check.
