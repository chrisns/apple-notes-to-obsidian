-- List all notes as TSV: id<TAB>name<TAB>created_iso<TAB>modified_iso<TAB>folder<TAB>attachment_count
-- Usage: osascript list_notes.applescript

tell application "Notes"
	set output to ""
	set noteList to notes
	set total to count of noteList
	repeat with i from 1 to total
		set n to item i of noteList
		try
			set folderName to name of (container of n)
		on error
			set folderName to ""
		end try
		set isoC to my isoDate(creation date of n)
		set isoM to my isoDate(modification date of n)
		set safeName to my sanitiseTabs(name of n)
		set output to output & (id of n) & tab & safeName & tab & isoC & tab & isoM & tab & folderName & tab & (count of attachments of n) & linefeed
	end repeat
	return output
end tell

on isoDate(d)
	set y to year of d as integer
	set m to (month of d as integer)
	set dd to day of d as integer
	set hh to hours of d as integer
	set mm to minutes of d as integer
	set ss to seconds of d as integer
	return (text -4 thru -1 of ("0000" & y)) & "-" & (text -2 thru -1 of ("00" & m)) & "-" & (text -2 thru -1 of ("00" & dd)) & "T" & (text -2 thru -1 of ("00" & hh)) & ":" & (text -2 thru -1 of ("00" & mm)) & ":" & (text -2 thru -1 of ("00" & ss))
end isoDate

on sanitiseTabs(s)
	set AppleScript's text item delimiters to tab
	set parts to text items of s
	set AppleScript's text item delimiters to " "
	set out to parts as text
	set AppleScript's text item delimiters to ""
	return out
end sanitiseTabs
