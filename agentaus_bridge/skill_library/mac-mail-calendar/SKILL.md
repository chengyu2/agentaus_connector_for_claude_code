---
name: mac-mail-calendar
description: On macOS, use AppleScript to find and read emails in Apple Mail (body, attachments, zip files of resumes or forms), save draft emails from a chosen account, and create Calendar events with invitations. Use when asked to search, read or reply to emails in Apple Mail, draft emails to a list of people, or schedule a meeting or interview and invite people. Covers the traps that make these look done when they are not - Calendar's AppleScript adds no attendees, and a slow or failed search is not a missing feature.
license: Apache-2.0
metadata:
  adapted-for: agentaus
---

# Apple Mail and Calendar with AppleScript

Placeholders in angle brackets: `<account>` is an account's name as Mail's sidebar shows it,
`<Name> <address>` is the user's own sender identity on that account, and so on. Find the
real values first (section 1); never guess them.

## Rules

- **Never send** email or invitations unless the user explicitly said to send. Save drafts.
- **Check every write by reading it back:** count the drafts in that account's `Drafts`,
  and read the fields of the event you just made through the reference `make` returned
  (the recipe in section 5 does this). A command that returns without an error has proved
  nothing. Do not search the calendar afterwards for the event you hold: that search is slow
  and tells you nothing more.
- **A failed command is not a missing feature.** `tell application "Mail" to get properties`
  fails, yet messages can be read. Try the recipes below before concluding anything.
- **Search narrowly:** one account, its mailboxes, a date window, then a sender or subject
  test. Reading every message of every mailbox takes minutes on a real mailbox.
- **One recipient per draft** when writing to several people, so no one sees the others'
  addresses.
- Write longer scripts to a `.applescript` file and run `osascript file.applescript args`.
  Quoting a long script inside `osascript -e '...'` breaks on the first apostrophe.

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
		set m to item 1 of (messages of mailbox "Inbox" of account acct whose date received > ((current date) - 30 * days) and sender contains who)
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
	set toAddr to item 1 of argv
	set subj to item 2 of argv
	set body to item 3 of argv
	set fromId to item 4 of argv -- exactly "<Name> <address>" of one of Mail's accounts
	tell application "Mail"
		set m to make new outgoing message with properties {subject:subj, content:body & return & return, visible:true, sender:fromId}
		tell m
			make new to recipient at end of to recipients with properties {address:toAddr}
			if (count of argv) > 4 then
				tell content to make new attachment with properties {file name:(POSIX file (item 5 of argv))} at after last paragraph
			end if
		end tell
		delay 3 -- let the attachment load
		save m
		delay 2
		close (first window whose name is subj) saving no
	end tell
end run
```

Run: `osascript draft.applescript "<to>" "<subject>" "<body>" "<Name> <address>" /path/invite.ics`.

- **Make the draft visible, save it once, and close its window.** Measured:
  - `visible:false` drafts never close: `close m` and `delete m` leave them open, they pile up
    (`tell application "Mail" to count outgoing messages` keeps growing), and at about 20 Mail
    stopped answering AppleScript at all until it was quit and reopened.
  - `save m` followed by `close m saving yes`, run for 17 people back to back, left 29 drafts:
    extra copies, some with no attachment and three with no recipient.
  - The recipe above left exactly one complete draft per run and no open window. Its window
    shows on screen for a few seconds.
- Check `count outgoing messages` before and after: it must not grow.
- Run one script against Mail at a time. Mail answers one AppleScript request at a time: a
  second script waits behind a long one and fails after two minutes with "AppleEvent timed out".
- If Mail stops answering even `count outgoing messages`, scripts cannot fix it. Ask the user
  before quitting Mail: open drafts they have not saved would be lost.
- The draft lands in the `Drafts` mailbox of the account that `sender` belongs to. A
  `sender` that matches no account puts it in the default account.

### Check: exactly one complete draft per person

Count the drafts with that subject: it should equal the number of people. If it is more,
or some are incomplete, keep each person's newest complete draft - one recipient, the
attachment - and delete the rest. List first, then delete:

```applescript
on run argv
	-- argv: account, exact subject, then "list" or "delete"
	set acct to item 1 of argv
	set subj to item 2 of argv
	set doDelete to (item 3 of argv) is "delete"
	set out to ""
	set kept to {}
	set doomed to {}
	with timeout of 600 seconds
		tell application "Mail"
			set mb to item 1 of (mailboxes of account acct whose name is "Drafts")
			set ds to (messages of mb whose subject is subj)
			set ds to reverse of ds -- newest first
			repeat with d in ds
				set rs to to recipients of d
				set addr to ""
				if (count rs) is 1 then set addr to address of item 1 of rs
				if (count rs) is 1 and (count mail attachments of d) > 0 and kept does not contain addr then
					set end of kept to addr
					set out to out & "KEEP   " & addr & linefeed
				else
					set end of doomed to contents of d
					set out to out & "DELETE " & addr & " (recipients " & (count rs) & ", attachments " & (count mail attachments of d) & ")" & linefeed
				end if
			end repeat
			if doDelete then
				repeat with d in doomed
					delete d
				end repeat
			end if
		end tell
	end timeout
	if doDelete then return out & "kept " & (count kept) & ", deleted " & (count doomed)
	return out & "kept " & (count kept) & ", would delete " & (count doomed) & " (run again with delete)"
end run
```

Run `osascript dedupe.applescript "<account>" "<subject>" list`, read it, then run it again
with `delete`. Drafts that had no attachment by design are deleted too: drop the attachment
test (`(count mail attachments of d) > 0`) when the drafts carry none.

## 5. Send drafts - only when the user said to send

AppleScript cannot send a saved draft: `open` shows it in a reading window, with nothing to
send. Rebuild it as a new message - same sender, recipient, subject, text and attachments -
send that, then delete the draft. One person at a time:

```applescript
on run argv
	-- argv: account, exact subject, recipient address, a scratch folder
	set acct to item 1 of argv
	set subj to item 2 of argv
	set toAddr to item 3 of argv
	set dest to item 4 of argv
	with timeout of 180 seconds
		tell application "Mail"
			set mb to item 1 of (mailboxes of account acct whose name is "Drafts")
			set ds to (messages of mb whose subject is subj)
			set d to missing value
			repeat with x in ds
				if (count to recipients of x) is 1 and address of item 1 of to recipients of x is toAddr then set d to contents of x
			end repeat
			if d is missing value then return "NO DRAFT for " & toAddr
			set body to content of d
			set fromId to sender of d
			set fileList to {}
			repeat with a in mail attachments of d
				set p to dest & "/" & (name of a)
				save a in POSIX file p
				set end of fileList to p
			end repeat
			-- the draft's text already ends with the signature: do not add it again
			set om to make new outgoing message with properties {subject:subj, content:body, visible:true, sender:fromId}
			set message signature of om to missing value
			tell om
				make new to recipient at end of to recipients with properties {address:toAddr}
				repeat with p in fileList
					tell content to make new attachment with properties {file name:(POSIX file p)} at after last paragraph
				end repeat
			end tell
			delay 3
			if (count attachments of content of om) is not (count fileList) then return "ATTACHMENT MISSING for " & toAddr
			send om
			delay 2
			delete d
			return "SENT " & toAddr & " (" & (count fileList) & " attachment)"
		end tell
	end timeout
end run
```

Run: `osascript send.applescript "<account>" "<exact subject>" "<recipient>" /path/to/scratch/folder`,
once per recipient. It prints `SENT <address>`; anything else means that one was not sent.

- `files` is a reserved word inside `tell application "Mail"`: name a list of paths
  `fileList`, or the script stops with "Can't set every file".
- The draft's text already ends with the signature, so the rebuilt message adds none
  (`message signature ... missing value`). The signature's logo image is not carried over.
- **After each send, look for a prompt** before the next one: list Mail's windows and their
  sheets (`tell application "System Events" to tell process "Mail" to get count sheets of
  every window`). A "Save / Don't Save / Cancel" sheet means a message window was closed
  unsaved - stop and tell the user rather than clicking it.
- Check at the end: the number of messages with that subject in `Sent Items` (or
  `Sent Mail`) grew by the number of people, and none are left in `Drafts`.

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
event's calendar, start and location; and that nothing was sent, so the invitations go out
when the user sends the drafts.
