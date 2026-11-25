-- Send one saved draft by rebuilding it as a new message, then delete the draft once the
-- message is in the Sent mailbox. Used by the mac-mail-calendar skill, section 5.
-- "preview" changes nothing; "send" sends.
-- osascript send-draft.applescript <account> <exact subject> <recipient> <attachments> <Sent mailbox> <scratch folder> preview|send

on run argv
	-- argv: account, exact subject, recipient, attachments the draft should have,
	--       the Sent mailbox's name, an empty scratch folder, then "preview" or "send"
	set acct to item 1 of argv
	set subj to item 2 of argv
	set toAddr to item 3 of argv
	set wanted to (item 4 of argv) as integer
	set sentName to item 5 of argv
	set dest to item 6 of argv
	set doSend to (item 7 of argv) is "send"
	with timeout of 600 seconds
		tell application "Mail"
			if (count (mailboxes of account acct whose name is sentName)) is 0 then return "NOT SENT " & toAddr & " - no mailbox " & sentName
			set sentBox to item 1 of (mailboxes of account acct whose name is sentName)
			set mb to item 1 of (mailboxes of account acct whose name is "Drafts")
			set ids to {}
			repeat with x in (messages of mb whose subject is subj)
				if (count to recipients of x) is 1 and address of item 1 of to recipients of x is toAddr then set end of ids to id of x
			end repeat
			if (count ids) is 0 then return "NOT SENT " & toAddr & " - no draft"
			if (count ids) > 1 then return "NOT SENT " & toAddr & " - " & (count ids) & " drafts, run the clean-up first"
			set d to first message of mb whose id is (item 1 of ids)
			set nAtt to count mail attachments of d
			if nAtt < wanted then return "NOT SENT " & toAddr & " - the draft has " & nAtt & " attachment(s), expected " & wanted
			if (count (windows whose name is subj)) > 0 then return "NOT SENT " & toAddr & " - a window titled " & subj & " is open"
			set ccs to {}
			repeat with r in cc recipients of d
				set end of ccs to address of r
			end repeat
			set bccs to {}
			repeat with r in bcc recipients of d
				set end of bccs to address of r
			end repeat
			set attNames to {}
			repeat with a in mail attachments of d
				set end of attNames to name of a
			end repeat
			set body to content of d
			set fromId to sender of d
			if not doSend then
				return "WILL SEND " & toAddr & linefeed & "  from: " & fromId & linefeed & "  cc: " & my joined(ccs) & "  bcc: " & my joined(bccs) & linefeed & "  attachments: " & my joined(attNames) & linefeed & "  text: " & my opening(body)
			end if
			-- each attachment in its own folder, so two with the same name cannot collide
			set fileList to {}
			set i to 0
			repeat with a in mail attachments of d
				set i to i + 1
				set p to dest & "/" & i & "/" & (name of a)
				my makeFolder(dest & "/" & i)
				save a in POSIX file p
				set end of fileList to p
			end repeat
			-- the draft's text already ends with the signature: do not add it again
			set om to make new outgoing message with properties {subject:subj, content:body, visible:true, sender:fromId}
			set message signature of om to missing value
			tell om
				make new to recipient at end of to recipients with properties {address:toAddr}
				repeat with x in ccs
					make new cc recipient at end of cc recipients with properties {address:(contents of x)}
				end repeat
				repeat with x in bccs
					make new bcc recipient at end of bcc recipients with properties {address:(contents of x)}
				end repeat
				repeat with p in fileList
					tell content to make new attachment with properties {file name:(POSIX file (contents of p))} at after last paragraph
				end repeat
			end tell
			delay 3
			set n to 0
			repeat 30 times
				set n to count attachments of content of om
				if n ≥ (count fileList) then exit repeat
				delay 0.5
			end repeat
			if n < (count fileList) then
				close (first window whose name is subj) saving no
				return "NOT SENT " & toAddr & " - the attachments did not load; the draft is kept"
			end if
			set startTime to (current date) - 30
			set sendNote to ""
			try
				if (send om) is false then set sendNote to " (Mail reported the send failed)"
			on error e
				set sendNote to " (send: " & e & ")"
			end try
			-- delete the draft only once the message is in the Sent mailbox
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
			if not found then return "UNCONFIRMED " & toAddr & sendNote & " - not in " & sentName & " after 60 seconds; the draft is kept"
			delete d
			return "SENT " & toAddr & " (" & (count fileList) & " attachment); in " & sentName & ", draft deleted"
		end tell
	end timeout
end run

on joined(xs)
	set AppleScript's text item delimiters to ","
	set s to xs as text
	set AppleScript's text item delimiters to ""
	return s
end joined

on opening(s)
	if length of s > 200 then return (text 1 thru 200 of s) & "..."
	return s
end opening

on makeFolder(p)
	do shell script "mkdir -p " & quoted form of p
end makeFolder
