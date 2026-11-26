-- Send one message from Apple Mail, built fresh (not from a saved draft), with Mail's own signature.
-- Used by the mac-mail-calendar skill, section 5.
--   * Mail's signature (with its logo) is added by Mail: the body file ends with the short sign-off
--     ("Thanks,\n<first name>"), never a typed name/title/company block.
--   * Mail puts an empty first line above any body a script sets. It can only be removed with the
--     window in front (Cmd-Up, then Forward Delete), so Mail comes forward for a few seconds:
--     tell the user to keep hands off the keyboard before a batch.
--   * Just before sending, the first line, To, Cc, Bcc and subject are read back: stray typing can
--     change them while the window is in front. Any mismatch discards the message unsent.
--   * After sending, a Mail warning sheet (invalid address...) means NOT SENT.
-- osascript send-message.applescript <account> <subject> <to> <cc or ""> <body.txt> <attachment or ""> <signature name> <Sent mailbox> [nowait]
-- Prints SENT / NOT SENT / UNCONFIRMED. With "nowait", confirm the batch in the Sent mailbox afterwards.
on run argv
	set acct to item 1 of argv
	set subj to item 2 of argv
	set toAddr to item 3 of argv
	set ccAddr to item 4 of argv
	set body to read POSIX file (item 5 of argv) as «class utf8»
	set attPath to item 6 of argv
	set sigName to item 7 of argv
	set sentName to item 8 of argv
	set firstWord to word 1 of body
	with timeout of 600 seconds
		tell application "Mail"
			if (count (windows whose name is subj)) > 0 then return "NOT SENT " & toAddr & " - a window titled " & subj & " is open"
			if (count (mailboxes of account acct whose name is sentName)) is 0 then return "NOT SENT " & toAddr & " - no mailbox " & sentName
			set sentBox to item 1 of (mailboxes of account acct whose name is sentName)
			-- send from this account's own identity: "<full name> <first address>"
			set es to email addresses of account acct
			set fromId to (full name of account acct) & " <" & (item 1 of es) & ">"
			set om to make new outgoing message with properties {subject:subj, content:body, visible:true, sender:fromId}
			set message signature of om to signature sigName
			tell om
				make new to recipient at end of to recipients with properties {address:toAddr}
				if ccAddr is not "" then make new cc recipient at end of cc recipients with properties {address:ccAddr}
				if attPath is not "" then tell content to make new attachment with properties {file name:(POSIX file attPath)} at after last paragraph
			end tell
			set n to 0
			if attPath is not "" then
				repeat 30 times
					delay 0.5
					set n to count attachments of content of om
					if n ≥ 1 then exit repeat
				end repeat
				if n < 1 then
					my discard(subj)
					return "NOT SENT " & toAddr & " - the attachment did not load"
				end if
			end if
			delay 1.5
		end tell
		-- remove the empty first line Mail puts above a scripted body
		set done to false
		repeat 10 times
			tell application "System Events" to tell process "Mail"
				set frontmost to true
				delay 0.4
				set wa to missing value
				try
					set w to (first window whose name is subj)
					perform action "AXRaise" of w
					set wa to my findWebArea(w, 0)
				end try
			end tell
			tell application "System Events"
				set fp to name of (first process whose frontmost is true)
				if wa is not missing value and fp is "Mail" then
					set focused of wa to true
					delay 0.3
					set fp to name of (first process whose frontmost is true)
					if fp is "Mail" then
						key code 126 using command down
						delay 0.2
						key code 117
						delay 0.5
						set done to true
					end if
				end if
			end tell
			if done then exit repeat
			delay 1
		end repeat
		if not done then
			my discard(subj)
			return "NOT SENT " & toAddr & " - could not focus the message body"
		end if
		tell application "Mail"
			set c to content of om
			if (count paragraphs of c) < 1 or (paragraph 1 of c) does not start with firstWord then
				my discard(subj)
				return "NOT SENT " & toAddr & " - first line is not the greeting: [" & (paragraph 1 of c) & "]"
			end if
			-- recipients must be exactly what we set: stray typing can change them while the window is in front
			set toNow to {}
			repeat with r in to recipients of om
				set end of toNow to address of r
			end repeat
			set ccNow to {}
			repeat with r in cc recipients of om
				set end of ccNow to address of r
			end repeat
			set ccWanted to {}
			if ccAddr is not "" then set ccWanted to {ccAddr}
			if toNow is not {toAddr} or ccNow is not ccWanted or (count bcc recipients of om) > 0 then
				my discard(subj)
				return "NOT SENT " & toAddr & " - recipients changed: to " & my joined(toNow) & " cc " & my joined(ccNow)
			end if
			if subject of om is not subj then
				my discard(subj)
				return "NOT SENT " & toAddr & " - subject changed"
			end if
			set startTime to (current date) - 30
			set sendNote to ""
			try
				if (send om) is false then set sendNote to " (Mail reported the send failed)"
			on error e
				set sendNote to " (send: " & e & ")"
			end try
			-- a warning sheet (an invalid address, a missing subject) stops the send without an error
			delay 1.5
			set sheetText to my sheetOn(subj)
			if sheetText is not "" then
				try
					tell application "System Events" to tell process "Mail" to click button "Cancel" of sheet 1 of (first window whose name is subj)
				end try
				my discard(subj)
				return "NOT SENT " & toAddr & " - Mail warned: " & sheetText
			end if
			-- with "nowait", check Sent Items once for the whole batch afterwards, so Mail is not held in front
			if (count argv) > 8 and item 9 of argv is "nowait" then return "SENT " & toAddr & sendNote
			set found to false
			repeat 60 times
				delay 1
				repeat with x in (messages of sentBox whose subject is subj and date sent ≥ startTime)
					repeat with r in to recipients of x
						if address of r is toAddr then set found to true
					end repeat
				end repeat
				if found then exit repeat
			end repeat
			if not found then return "UNCONFIRMED " & toAddr & sendNote & " - not in " & sentName & " after 60 seconds"
			return "SENT " & toAddr & sendNote
		end tell
	end timeout
end run

-- close a compose window without keeping it: "close ... saving no" can still raise the
-- Save / Don't Save sheet, so answer it with Don't Save
on discard(subj)
	try
		tell application "Mail" to close (first window whose name is subj) saving no
	end try
	delay 1
	try
		tell application "System Events" to tell process "Mail"
			set w to (first window whose name is subj)
			if (count sheets of w) > 0 then click button "Don’t Save" of sheet 1 of w
		end tell
	end try
end discard

-- find the message body (AXWebArea); "entire contents" returns nothing for Mail's compose window
on findWebArea(e, depth)
	if depth > 6 then return missing value
	tell application "System Events"
		try
			set kids to UI elements of e
		on error
			return missing value
		end try
		repeat with k in kids
			set r to ""
			try
				set r to role of k
			end try
			if r is "AXWebArea" then return contents of k
			if r is "AXGroup" or r is "AXScrollArea" or r is "AXSplitGroup" then
				set f to my findWebArea(k, depth + 1)
				if f is not missing value then return f
			end if
		end repeat
	end tell
	return missing value
end findWebArea

on joined(xs)
	set AppleScript's text item delimiters to ","
	set t to xs as text
	set AppleScript's text item delimiters to ""
	return t
end joined

on sheetOn(subj)
	try
		tell application "System Events" to tell process "Mail"
			set w to (first window whose name is subj)
			if (count sheets of w) > 0 then
				set AppleScript's text item delimiters to " "
				set t to (value of every static text of sheet 1 of w) as text
				set AppleScript's text item delimiters to ""
				if t is "" then set t to "(a dialog)"
				return t
			end if
		end tell
	end try
	return ""
end sheetOn
