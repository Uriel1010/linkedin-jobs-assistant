"""Explicit UI translations; never translate or rewrite source job content."""
import re

COMMAND_ALIASES={'התחלה':'start','עזרה':'help','מצב':'status','משרות':'jobs','חדש':'new',
    'העדפות':'preferences','שקט':'quiet','שפה':'language','עקוב':'track','חיפושים':'searches',
    'ערוך_חיפוש':'setsearch','אסוף':'collect','השהה':'pause','המשך':'resume','התראות':'alerts',
    'הודעות':'notify','משרה':'job','תיאור':'description','רענן':'refresh','שמור':'save',
    'מועמדויות':'applications','שלב':'stage','תזכורות':'followups'}
KEY_ALIASES={'תפקידים':'roles','רמות':'levels','אזור':'region','גיל':'age',
    'לא_ידוע':'unknown','כלול':'include','החרג':'exclude'}
VALUE_ALIASES={'תוכנה':'software','משובצת':'embedded','אמבדד':'embedded','בדיקות':'qa',
    'אחר':'other','הכל':'all','נתונים':'data','דאטה':'data','אבטחה':'security','תמיכה':'support',
    'מחקר':'research','ניהול':'management','למידת_מכונה':'ml','בקאנד':'backend','פרונטאנד':'frontend',
    'פולסטאק':'fullstack','ביניים':'mid','בכיר':'senior','בכירה':'senior','מוביל':'lead',
    'זוטר':'junior','זוטרה':'junior','גוניור':'junior','מתמחה':'intern','פרינסיפל':'principal',
    'ישראל':'israel','רחב':'broad','שמור':'keep','הסתר':'hide','נקה':'clear'}
VALUE_ALIASES.update({'מערכות משובצות':'embedded','משובצות':'embedded','למידת מכונה':'ml',
    'צד לקוח':'frontend','צד שרת':'backend','פול סטאק':'fullstack'})

HE={
    'language_prompt':'שפת הבוט: {selected}\n\nבחרו עברית, אנגלית או זיהוי אוטומטי לפי Telegram.\n/language he · /language en · /language auto',
    'Use /language he, /language en or /language auto.':'לבחירת שפה: /language he או /language en או /language auto.',
    'roles':'תפקידים','levels':'רמות','region':'אזור','age':'גיל','unknown':'ותק לא ידוע','include':'כלול','exclude':'החרג',
    'Specify {key} only once. Nothing changed.':'יש לציין את {key} פעם אחת בלבד. דבר לא השתנה.',
    'Choose {key}: {choices}. Nothing changed.':'בחרו {key}: {choices}. דבר לא השתנה.',
    'language_name_he':'עברית','language_name_en':'English','language_name_auto':'אוטומטי',
    'New jobs':'משרות חדשות','Preferences':'העדפות','Searches':'חיפושים','Feed status':'מצב המערכת',
    'Help':'עזרה','Language':'שפה','Pause alerts':'השהיית הודעות','Resume alerts':'חידוש הודעות',
    'Previous':'הקודם','Next':'הבא','‹ Previous':'‹ הקודם','Next ›':'הבא ›',
    'Fetch description':'טעינת תיאור','Save job':'שמירת משרה','Turn off':'כיבוי',
    'Software roles':'תפקידי תוכנה','All roles':'כל התפקידים','All seniorities':'כל רמות הוותק',
    'Junior':'זוטר','Senior':'בכיר','Lead / Principal':'מוביל / פרינסיפל',
    'Israel only':'ישראל בלבד','Broader locations':'מיקומים נוספים',
    'software':'תוכנה','embedded':'מערכות משובצות','qa':'בדיקות','other':'אחר','all':'הכל',
    'backend':'צד שרת','frontend':'צד לקוח','fullstack':'פול סטאק','data':'נתונים','devops':'DevOps',
    'ml':'למידת מכונה','security':'אבטחה','support':'תמיכה','research':'מחקר','management':'ניהול',
    'intern':'מתמחה','junior':'זוטר','mid':'ביניים','senior':'בכיר','lead':'מוביל','principal':'פרינסיפל',
    'saved':'נשמרה','reviewing':'בבדיקה','applied':'הוגשה מועמדות','interviewing':'ראיונות',
    'offer':'הצעה','rejected':'נדחתה','withdrawn':'בוטלה','archived':'בארכיון',
    'Any location':'כל מיקום','Company unknown':'שם החברה לא זמין','Location unknown':'המיקום לא זמין',
    'Location not specified':'המיקום לא צוין','Job':'משרה','Not yet':'טרם בוצע',
    'Hourly update':'עדכון שעתי','Catch-up after quiet hours':'השלמה לאחר השעות השקטות',
    'Israel time':'שעון ישראל','Page':'עמוד','newly found job':'משרה שהתגלתה','newly found jobs':'משרות שהתגלו',
    'Posted on LinkedIn: {date}':'פורסם ב-LinkedIn: {date}',
    'LinkedIn label: {label}':'תווית המקור ב-LinkedIn: {label}',
    ' · observed {time}':' · נצפה ב-{time}',
    ' · observation time unknown':' · זמן התצפית לא ידוע',
    'LinkedIn: {label} · original posting date unknown':'LinkedIn: {label} · מועד הפרסום המקורי לא ידוע',
    'Posting date not provided by LinkedIn':'LinkedIn לא סיפקה תאריך פרסום',
    ' (title/metadata)':' (לפי הכותרת או נתוני המקור)',
    'Your job feed · Israel time':'פיד המשרות שלך · שעון ישראל',
    'Delivery: {state}':'שליחת הודעות: {state}','On':'פעילה','Paused':'מושהית',
    'Quiet hours: {state}':'שעות שקטות: {state}',
    'Active · automatic messages queued':'פעילות · הודעות אוטומטיות ממתינות',
    'Scheduled · /quiet':'מוגדרות · /quiet','Off · /quiet to choose':'כבויות · לבחירה /quiet',
    'Roles: {roles} · Levels: {levels}':'תפקידים: {roles} · רמות: {levels}',
    'Location rule: {rule}':'כלל מיקום: {rule}',
    'Israel explicitly listed':'ישראל מצוינת במפורש','Broader locations allowed':'מיקומים נוספים מותרים',
    'Sign-in required · open /alerts':'נדרשת התחברות · /alerts',
    'Cooling down; the server will retry automatically':'בהמתנה עקב הגבלת קצב; השרת ינסה שוב אוטומטית',
    'Working at the last successful search':'פעל בחיפוש האחרון שהצליח',
    'Awaiting the first successful search':'ממתין לחיפוש הראשון שיצליח',
    'Last successful search: {time}':'החיפוש האחרון שהצליח: {time}',
    'Next search: {time}':'החיפוש הבא: {time}',
    'Coverage: limited · some results may be missing':'כיסוי חלקי · ייתכן שחסרות תוצאות',
    'Collection is paused. Use /resume to restart your search.':'האיסוף מושהה. לחידוש החיפוש: /resume',
    'Saved jobs: {jobs} · Filtered from feed: {filtered}':'משרות במאגר: {jobs} · סוננו מהפיד: {filtered}',
    'Waiting to send: {count}':'ממתינות לשליחה: {count}',
    'Last delivery: {time}':'השליחה האחרונה: {time}',
    'Descriptions: {pending} preparing · {failed} unavailable':'תיאורים: {pending} בהכנה · {failed} לא זמינים',
    'Issues: {issues} · Follow-ups due: {due}':'תקלות פתוחות: {issues} · תזכורות שהגיע זמנן: {due}',
    'Quiet hours':'שעות שקטות','Quiet now':'השעות השקטות פעילות כעת',
    'Outside quiet hours':'כעת מחוץ לשעות השקטות','Off · choose your hours below':'כבוי · בחרו שעות למטה',
    'quiet_explanation':'האיסוף ממשיך. הודעות אוטומטיות ממתינות לסיום השעות השקטות, ולאחר מכן המשרות שנאספו מגיעות יחד עם כפתורי עמודים. פקודות זמינות בכל שעה.\n\nשעות לבחירתך: /quiet 23:00 08:00\nאזור זמן אופציונלי: /quiet 23:00 08:00 Europe/London',
    'preferences_summary':'העדפות הפיד\n\nתפקידים: {roles}\nרמות ותק: {levels}\nמיקום: {region}\nמגבלת גיל הפרסום: {age} שעות, כאשר יש מידע מתאים במקור\nותק לא ידוע: {unknown}\nמילים נדרשות בכותרת: {include}\nמילים להחרגה: {exclude}\n\nהסינון מבוסס על הכותרת והמיקום, ואינו ציון התאמה לקורות חיים. תאריכי פרסום חסרים נשארים לא ידועים.\n\nאפשר לשלב הגדרות:\n/preferences roles software,embedded; levels mid,senior; age 72\n\nאפשר גם בעברית:\nהעדפות תפקידים תוכנה,משובצת; רמות ביניים,בכיר; אזור ישראל\n\nכל ההגדרות נבדקות לפני השמירה. משרות שסוננו וטרם נשלחו נבדקות מחדש; משרות שכבר נשלחו אינן חוזרות.',
    'Israel must be explicitly listed':'ישראל חייבת להופיע במפורש','Broader regions and remote locations allowed':'מותרים גם אזורים רחבים ומיקומים מרוחקים',
    'kept':'נכלל','hidden':'מוסתר','Any':'ללא הגבלה','None':'אין',
    'Searches updated · {created} added · {existing} already tracked':'החיפושים עודכנו · {created} נוספו · {existing} כבר קיימים',
    'Hourly collection enabled':'איסוף שעתי הופעל','Already tracked':'החיפוש כבר קיים',
    'Already tracked · paused':'החיפוש כבר קיים · מושהה',
    'New searches start in the background. Existing schedules and pause settings are preserved. Use /searches to manage them.':'חיפושים חדשים מתחילים ברקע. לוחות זמנים קיימים והשהיות נשמרים. לניהול: /searches',
    'Searches · Israel time':'חיפושים · שעון ישראל','Active':'פעיל','Paused search':'מושהה',
    'Every {minutes} minutes':'כל {minutes} דקות','Coverage: {status}':'כיסוי: {status}',
    'Limited':'חלקי','ok':'תקין','pending':'ממתין','running':'פועל','partial':'חלקי','error':'שגיאה',
    'Stored jobs {start}–{end} of {total}':'משרות במאגר {start}–{end} מתוך {total}',
    'No stored jobs match this search. Use /track job title | location to start collecting.':'לא נמצאו משרות במאגר התואמות לחיפוש. להתחלת איסוף: /track תפקיד | מיקום',
    'No collection searches yet. Use /track job title | location.':'אין עדיין חיפושי איסוף. להוספה: /track תפקיד | מיקום',
    'No new jobs delivered yet. The hourly feed stays quiet when nothing new is found.':'טרם נשלחו משרות חדשות. כשאין משרות חדשות, הפיד השעתי נשאר שקט.',
    'Fetching the job description from LinkedIn…':'טוען את תיאור המשרה מ-LinkedIn…',
    'Full description: /description {id}':'התיאור המלא: /description {id}',
    'Description: {status}. Use the fetch button below.':'מצב התיאור: {status}. אפשר לטעון באמצעות הכפתור למטה.',
    'Saved to your tracker. Application ID: {id}\n{title}':'נשמרה במעקב. מספר מועמדות: {id}\n{title}',
    'This job is already in your tracker. See /applications.':'המשרה כבר במעקב שלך. /applications',
    'Follow-ups due':'תזכורות שהגיע זמנן','Applications':'מועמדויות','Follow-up: {time}':'תזכורת: {time}',
    'Tracker #{id}: {status}\n{title}':'מועמדות #{id}: {status}\n{title}',
    'Search updated. The next collection will use the new criteria; previously delivered jobs will not be repeated.':'החיפוש עודכן. האיסוף הבא ישתמש בתנאים החדשים; משרות שכבר נשלחו לא יישלחו שוב.',
    'Search {id} resumed.':'חיפוש {id} חודש.','Search {id} paused.':'חיפוש {id} הושהה.',
    'Collecting jobs… This can take a few minutes.':'אוסף משרות… הפעולה עשויה להימשך מספר דקות.',
    'Found: {found}\nNew matches: {new}\nPages: {pages}\nCoverage limited: {limited}\nWarning: {warning}':'נמצאו: {found}\nהתאמות חדשות: {new}\nעמודים: {pages}\nכיסוי חלקי: {limited}\nאזהרה: {warning}',
    'Last seen: {time}':'נצפה לאחרונה: {time}','No open issues.':'אין תקלות פתוחות.',
    'Unknown command. Use /help.':'הפקודה לא מוכרת. לעזרה: /help',
    'Feed notice':'הודעת מערכת','Follow-up due: #{id} · {title}\n{url}':'הגיע הזמן למעקב: #{id} · {title}\n{url}',
    'Search #{id} is overdue: {keywords}. Check /alerts and /status; LinkedIn may need reconnection.':'חיפוש #{id} מתעכב: {keywords}. בדקו /alerts ו-/status; ייתכן שנדרשת התחברות מחדש ל-LinkedIn.',
    'The jobs service has recovered. Your hourly feed and queued deliveries are running again.':'שירות המשרות חזר לפעול. הפיד השעתי וההודעות הממתינות פועלים שוב.',
    'The jobs API is not responding. New-job delivery is paused; queued jobs are retained. The server will keep retrying.':'שירות המשרות אינו מגיב. שליחת משרות חדשות מושהית וההודעות נשמרות בתור. השרת ימשיך לנסות.',
    'Request could not complete ({status}): {detail}\nTry again later or check /status.':'לא ניתן להשלים את הבקשה ({status}): {detail}\nנסו מאוחר יותר או בדקו /status.',
    'Use /notify on or /notify off.':'השתמשו ב-/notify on להפעלה או /notify off להשהיה.',
    'Use a numeric ID.':'יש להזין מספר מזהה.',
    'Unknown stage. See /help.':'שלב לא מוכר. לעזרה: /help',
    'Choose a search ID from /searches.':'בחרו מזהה חיפוש מתוך /searches.',
    'This update is no longer available.':'העדכון אינו זמין עוד.',
    'This page does not exist.':'העמוד אינו קיים.',
    'This update is no longer available. Use /new.':'העדכון אינו זמין עוד. לפתיחת העדכון האחרון: /new',
    'This browsing session expired. Use /jobs again.':'החיפוש הסתיים. התחילו שוב באמצעות /jobs.',
    'Notifications on. New jobs, important alerts, and due follow-ups are checked once a minute; existing jobs are not resent.':'ההודעות הופעלו. משרות חדשות, התראות חשובות ותזכורות נבדקות מדי דקה; משרות קיימות אינן נשלחות שוב.',
    'Notifications paused. Collection continues, and queued new jobs will remain saved until you resume.':'ההודעות הושהו. האיסוף ממשיך, והמשרות החדשות נשמרות בתור עד לחידוש.',
    'Too much historical data to initialize notifications. Contact the server administrator.':'יש יותר מדי נתונים היסטוריים לאתחול ההודעות. יש לפנות למנהל השרת.',
    'Search coverage is limited. Some jobs may be missing. Open /searches to narrow the search or inspect its latest run.':'כיסוי החיפוש חלקי וייתכן שחסרות משרות. אפשר לצמצם את החיפוש באמצעות /searches או לבדוק את הריצה האחרונה.',
    'LinkedIn needs you to sign in again. Collection is paused; saved jobs and queued messages are safe.':'נדרשת התחברות מחדש ל-LinkedIn. האיסוף מושהה; המשרות וההודעות הממתינות נשמרות.',
    'LinkedIn asked the collector to slow down. It will retry after the cooldown; saved jobs are safe.':'LinkedIn ביקשה להאט את האיסוף. השרת ינסה שוב לאחר ההמתנה; המשרות נשמרות.',
    'A job description could not be refreshed. Its job link is still available; the server will retry later.':'לא ניתן היה לרענן תיאור משרה. הקישור עדיין זמין והשרת ינסה שוב בהמשך.',
    'Use /quiet 23:00 08:00 [timezone], or /quiet off. Example timezone: Asia/Jerusalem.':'השתמשו ב-/quiet 23:00 08:00 [אזור זמן] או /quiet off. דוגמה לאזור זמן: Asia/Jerusalem.',
    'Unknown timezone. Example: Asia/Jerusalem.':'אזור הזמן לא מוכר. דוגמה: Asia/Jerusalem.',
    'Start and end must differ. Use /quiet off to disable.':'שעת ההתחלה והסיום חייבות להיות שונות. לכיבוי: /quiet off',
    'Use /setsearch ID job title | location.':'השתמשו ב-/setsearch מזהה תפקיד | מיקום.',
    'Use /stage APP_ID stage. See /help for stages.':'השתמשו ב-/stage מזהה שלב. לרשימת השלבים: /help',
    'Use /track job title | location. Paste up to 20 lines in one message.':'השתמשו ב-/track תפקיד | מיקום. אפשר להדביק עד 20 שורות בהודעה אחת.',
    'Each line must be /track job title | location. Nothing added.':'כל שורה חייבת להיות במבנה /track תפקיד | מיקום. דבר לא נוסף.',
    'Use a non-empty title and at most 200 characters per title/location. Nothing added.':'יש להזין תפקיד שאינו ריק ועד 200 תווים לתפקיד או למיקום. דבר לא נוסף.',
    'Add a setting after /preferences, for example roles software; age 72.':'הוסיפו הגדרה אחרי /preferences, למשל roles software; age 72.',
    'Use setting value, for example roles software; levels senior; age 72. Nothing changed.':'השתמשו במבנה הגדרה ערך, למשל roles software; levels senior; age 72. דבר לא השתנה.',
    'Use /preferences roles software,data; levels senior,lead; region israel; age 72; unknown keep; include clear; exclude words. Nothing changed.':'השתמשו למשל ב-/preferences roles software,embedded; levels mid,senior; region israel; age 72; unknown keep; include clear. דבר לא השתנה.',
    'Maximum 20 searches; nothing added. Update or remove an existing search first.':'ניתן לשמור עד 20 חיפושים. דבר לא נוסף; יש לערוך או להסיר חיפוש קיים תחילה.',
    'Check /alerts for details.':'לפרטים נוספים: /alerts',
    'open_alerts':'יש בעיה באיסוף. בדקו /alerts ו-/status.',
    'Open your private job feed':'פתיחת פיד המשרות הפרטי',
    'Latest update with page navigation':'העדכון האחרון עם כפתורי עמודים',
    'Browse all stored jobs':'עיון בכל המשרות השמורות',
    'Choose roles, levels and location':'בחירת תפקידים, רמות ומיקום',
    'Choose quiet hours and timezone':'בחירת שעות שקטות ואזור זמן',
    'Feed and search health':'מצב הפיד והחיפושים','Your collection searches':'חיפושי האיסוף שלך',
    'Add search: title | location':'הוספת חיפוש: תפקיד | מיקום',
    'Edit search: ID title | location':'עריכת חיפוש: מזהה תפקיד | מיקום',
    'Run a search now':'הפעלת חיפוש כעת','Pause collection for a search':'השהיית איסוף לחיפוש',
    'Resume collection for a search':'חידוש איסוף לחיפוש','Turn notifications on or off':'הפעלה או השהיה של הודעות',
    'Job details: JOB_ID':'פרטי משרה לפי מזהה','Stored description: JOB_ID':'תיאור שמור לפי מזהה',
    'Fetch description: JOB_ID':'טעינת תיאור לפי מזהה','Track an application: JOB_ID':'שמירה למעקב לפי מזהה משרה',
    'Your application tracker':'מעקב המועמדויות שלך','Update application: APP_ID stage':'עדכון שלב במועמדות',
    'Due follow-ups':'תזכורות שהגיע זמנן','Collection alerts':'התראות איסוף',
    'All commands and examples':'כל הפקודות ודוגמאות','Choose English, Hebrew or automatic':'בחירת עברית, אנגלית או זיהוי אוטומטי',
}

WELCOME_EN='Your hourly job feed is ready. New discoveries arrive with links; existing jobs are not repeated.\n\nUse /new, /preferences or /status. Browse every stored job with /jobs.'
HE[WELCOME_EN]='פיד המשרות השעתי שלך מוכן. משרות חדשות מגיעות עם קישורים, בלי חזרה על משרות שכבר נשלחו.\n\n/new לעדכון האחרון, /preferences להעדפות, /status למצב המערכת ו-/jobs לעיון במאגר.\n\nלבחירת שפה: /language'

HELP_HE='''הבוט הפרטי לחיפוש משרות

/new — העדכון האחרון וכפתורי עמודים
/jobs [מילים] — חיפוש במאגר
/preferences — תפקידים, ותק, מיקום ומילות סינון
/quiet — שעות שקטות ואזור זמן
/language — בחירת שפה: he / en / auto
/status — מצב האיסוף וההודעות
/track תפקיד | מיקום — חיפוש שעתי; אפשר כמה שורות יחד
/searches — רשימת החיפושים והמזהים
/setsearch מזהה תפקיד | מיקום — עריכת חיפוש
/collect מזהה — איסוף כעת, בכפוף להגבלות
/pause מזהה או /resume מזהה — השהיית חיפוש או חידושו
/job מזהה — פרטי משרה
/description מזהה — תיאור מלא
/refresh מזהה — טעינת תיאור
/save מזהה — שמירה למעקב
/applications [שלב] — מועמדויות
/stage מזהה שלב — עדכון שלב
/followups — תזכורות
/alerts — תקלות באיסוף
/notify on או /notify off — הפעלה או השהיה של הודעות

אפשר לכתוב גם: עזרה, מצב, משרות, העדפות, שקט, שפה, עקוב וחיפושים.
דוגמה: עקוב מהנדס קושחה | ישראל
העדפות תפקידים משובצת,תוכנה; רמות ביניים,בכיר; גיל 72

המעקב אינו מגיש מועמדות או פונה למעסיקים. תוכן המשרות נשמר בשפת המקור.'''

EN={'language_prompt':'Bot language: {selected}\n\nChoose English, Hebrew or automatic detection from Telegram.\n/language he · /language en · /language auto',
    'language_name_he':'עברית','language_name_en':'English','language_name_auto':'Automatic'}

def tr(template,language='en',**fields):
    text=HE.get(template,EN.get(template,template)) if language=='he' else EN.get(template,template)
    return text.format(**fields) if fields else text

def source(value,language='en'):
    text=str(value)
    return '\u2068'+text+'\u2069' if language=='he' else text

def preference_value(value):
    return VALUE_ALIASES.get(value,value)

def command_alias(text):
    """Canonicalize only command prefixes, never search text or keywords."""
    result=[]
    for line in text.splitlines():
        parts=line.strip().split(maxsplit=1)
        if not parts:
            result.append('')
            continue
        first=parts[0].lstrip('/')
        replacement=COMMAND_ALIASES.get(first)
        result.append(('/'+replacement if replacement else parts[0])+(' '+parts[1] if len(parts)>1 else ''))
    return '\n'.join(result)

def is_hebrew_command(text):
    first=text.strip().split(maxsplit=1)
    return bool(first and first[0].lstrip('/') in COMMAND_ALIASES)


HE.update({'Reposted · original date unknown':'פורסם מחדש · התאריך המקורי לא ידוע',
    'Posted today':'פורסם היום','Posted yesterday':'פורסם אתמול','Date not listed':'תאריך לא צוין',
    'Hybrid':'היברידי','Remote':'מרחוק','On-site':'במשרד',
    'Matching your current preferences':'תואם להעדפות הנוכחיות שלך',
    'Tap a title to open LinkedIn.':'לחיצה על הכותרת פותחת את LinkedIn.',
    'All':'הכול','Choose a page':'בחירת עמוד','Back to jobs':'חזרה למשרות',
    'Uncategorized roles: related engineering only':'תפקידים לא מסווגים: הנדסה קרובה בלבד',
    'Uncategorized roles: all titles':'תפקידים לא מסווגים: כל הכותרות'})

HE.update({'Next digest: {time}':'העדכון הבא: {time}',
 'No jobs match your current preferences. Use /preferences to adjust.':'אין משרות שתואמות להעדפות הנוכחיות. לשינוי: /preferences',
 'Related engineering only':'הנדסה קרובה בלבד','All uncategorized titles':'כל הכותרות הלא מסווגות'})
KEY_ALIASES['אחרים']='other_scope'
VALUE_ALIASES['טכני']='technical'
HE['Notifications on. New jobs are grouped into hourly digests. Important alerts and due follow-ups are checked once a minute; quiet hours apply.']='ההתראות פעילות. משרות חדשות מרוכזות בעדכון שעתי. התראות חשובות ותזכורות נבדקות מדי דקה, בכפוף לשעות השקט.'
