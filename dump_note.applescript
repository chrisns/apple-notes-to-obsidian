-- Dump a single Apple Note as a record-style block.
-- Usage: osascript dump_note.applescript "<note-id>"
-- Output: NUL-delimited fields so HTML bodies (which contain newlines) survive intact.
--   Format: id\0name\0created_iso\0modified_iso\0folder\0body_html

on run argv
	set targetId to item 1 of argv
	tell application "Notes"
		set n to note id targetId
		try
			set folderName to name of (container of n)
		on error
			set folderName to ""
		end try
		set isoCreated to my isoDate(creation date of n)
		set isoModified to my isoDate(modification date of n)
		set NUL to (ASCII character 0)
		return (id of n) & NUL & (name of n) & NUL & isoCreated & NUL & isoModified & NUL & folderName & NUL & (body of n)
	end tell
end run

on isoDate(d)
	-- Build a YYYY-MM-DDTHH:MM:SS string in local time
	set y to year of d as integer
	set m to (month of d as integer)
	set dd to day of d as integer
	set hh to hours of d as integer
	set mm to minutes of d as integer
	set ss to seconds of d as integer
	return (text -4 thru -1 of ("0000" & y)) & "-" & (text -2 thru -1 of ("00" & m)) & "-" & (text -2 thru -1 of ("00" & dd)) & "T" & (text -2 thru -1 of ("00" & hh)) & ":" & (text -2 thru -1 of ("00" & mm)) & ":" & (text -2 thru -1 of ("00" & ss))
end isoDate
