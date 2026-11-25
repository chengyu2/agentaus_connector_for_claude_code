-- Clean up duplicate drafts: keep one per person, delete the extra copies.
-- Used by the mac-mail-calendar skill, section 4. Read-only unless the last argument is "delete".
-- osascript dedupe-drafts.applescript <account> <exact subject> <attachments each> <a@x.com,b@y.com> list|delete

on run argv
	-- argv: account, exact subject, attachments each draft should have (0 if none),
	--       the people drafted for, comma-separated without spaces, then "list" or "delete"
	set acct to item 1 of argv
	set subj to item 2 of argv
	set wanted to (item 3 of argv) as integer
	set AppleScript's text item delimiters to ","
	set people to text items of (item 4 of argv)
	set AppleScript's text item delimiters to ""
	set doDelete to (item 5 of argv) is "delete"
	-- pass 1: read each draft once
	set rows to {}
	with timeout of 600 seconds
		tell application "Mail"
			set mb to item 1 of (mailboxes of account acct whose name is "Drafts")
			repeat with d in (messages of mb whose subject is subj)
				set addrs to {}
				repeat with r in ((to recipients of d) & (cc recipients of d) & (bcc recipients of d))
					set end of addrs to address of r
				end repeat
				set end of rows to {mid:id of d, addrs:addrs, nTo:(count to recipients of d), nAtt:(count mail attachments of d)}
			end repeat
		end tell
	end timeout
	-- pass 2: choose the draft to keep for each person
	set out to ""
	set keepIds to {}
	repeat with p in people
		set best to missing value
		set bestOk to false
		repeat with r in rows
			if nTo of r is 1 and (count addrs of r) is 1 and item 1 of addrs of r is (contents of p) then
				set ok to (nAtt of r) ≥ wanted
				if best is missing value or (ok and not bestOk) or (ok is bestOk and mid of r > mid of best) then
					set best to contents of r
					set bestOk to ok
				end if
			end if
		end repeat
		if best is missing value then
			set out to out & "NONE      " & p & linefeed
		else
			set end of keepIds to mid of best
			set out to out & "KEEP      " & p & " (id " & (mid of best) & ", attachments " & (nAtt of best) & ")"
			if not bestOk then set out to out & " - MISSING ATTACHMENT, show the user"
			set out to out & linefeed
		end if
	end repeat
	-- pass 3: classify every other draft
	set doomed to {}
	repeat with r in rows
		if keepIds does not contain (mid of r) then
			set inScope to true
			repeat with a in addrs of r
				if people does not contain (contents of a) then set inScope to false
			end repeat
			set label to "id " & (mid of r) & " " & my joined(addrs of r) & " (attachments " & (nAtt of r) & ")"
			if not inScope then
				set out to out & "UNTOUCHED " & label & " - recipients not on the list" & linefeed
			else if (count addrs of r) is 0 then
				set end of doomed to mid of r
				set out to out & "DELETE    " & label & " - no recipient" & linefeed
			else if (count addrs of r) > 1 or nTo of r is not 1 then
				set out to out & "UNTOUCHED " & label & " - not one To recipient" & linefeed
			else
				set end of doomed to mid of r
				set out to out & "DELETE    " & label & " - another copy for this person is kept" & linefeed
			end if
		end if
	end repeat
	if not doDelete then return out & (count doomed) & " to delete; nothing changed"
	-- pass 4: delete by id, then read the mailbox again
	set failed to 0
	with timeout of 600 seconds
		tell application "Mail"
			repeat with i in doomed
				try
					delete (first message of mb whose id is (contents of i))
				on error
					set failed to failed + 1
				end try
			end repeat
			set remaining to count (messages of mb whose subject is subj)
		end tell
	end timeout
	return out & "deleted " & ((count doomed) - failed) & ", failed " & failed & ", drafts with this subject now " & remaining
end run

on joined(xs)
	set AppleScript's text item delimiters to ","
	set s to xs as text
	set AppleScript's text item delimiters to ""
	return s
end joined
