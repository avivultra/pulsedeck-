# Health Janitor — שלב 1: זיהוי conhost זומבים

## מטרה
מודול רקע שמזהה תהליכי `conhost.exe` שהצטברו כזומבים תחת תהליכי dev (claude, electron, node), מציג אינדיקטור `🧹 N` בדוק, ופותח חלון ניקוי בלחיצה. הריגה רק באישור משתמש.

## נוהל בדיקה אחרי כל שלב
1. הרץ `python -m pytest test_monitor.py -q` — חייב לעבור
2. הרץ `python -c "import <module>"` עבור כל קובץ שעודכן/נוצר — חייב לייבא בלי שגיאות
3. סמן ✅ ליד השלב כאן, commit נקודתי
4. אם משהו נשבר — תקן לפני המעבר לשלב הבא

---

## שלבים

### ☐ שלב 0 — checkpoint git לפני התחלה
- [x] commit של כל העבודה הקיימת (`4b76fde`)
- [x] קובץ תכנית `JANITOR_PLAN.md` נוצר

### ✅ שלב 1 — `janitor.py` (מודול חדש)
- [x] `ZombieGroup` dataclass עם השדות: `parent_name`, `parent_pid`, `zombie_pids`, `total_rss_bytes`, `scanned_at`
- [x] `JanitorScanner` class עם `start()`, `stop()`, `_run()`, `scan()`
- [x] `count_total_zombies()` — סכום מהיר מהמטמון לאינדיקטור
- [x] `kill_group(group)` עם הגנה על `os.getpid()` ו-`PROTECTED_NAMES`
- [x] audit log ייעודי `history/janitor.log` עם `RotatingFileHandler` (512KB × 3)
- [x] `get_default_janitor()` — singleton lazy
- [x] `open_cleanup_panel(parent)` — חלון Tk עם רשימת קבוצות וכפתורי ניקוי

**בדיקה:** ✅ imports OK · 29/29 tests pass

### ✅ שלב 2 — `config.py` (סעיף `janitor`)
- [x] הוספת `janitor` ל-`DEFAULT_CONFIG`
- [x] בדיקה אוטומטית ב-tempdir אישרה שהסעיף נכתב לקובץ ונקרא בחזרה

**בדיקה:** ✅ verified via temp config — section saves and loads correctly

### ✅ שלב 3 — Unit tests ב-`test_monitor.py`
- [x] mock של 25 conhost עם parent claude.exe → ZombieGroup אחד עם 25
- [x] 19 conhost מאותו הורה → רשימה ריקה (מתחת לסף)
- [x] conhost עם `parent.pid == os.getpid()` → לא נכלל
- [x] הורה לא חשוד (explorer.exe) → לא נכלל
- [x] `count_total_zombies()` מסכם נכון על מספר קבוצות
- [x] `total_rss_bytes` מצטבר מכל ה-PIDs בקבוצה

**בדיקה:** ✅ 5 טסטים חדשים, סך 34/34 עוברים

### ✅ שלב 4 — חלון ניקוי `open_cleanup_panel`
- [x] Toplevel 560×460, ערכת צבעים תואמת (BG #0d1117 + AMBER accent)
- [x] רשימת קבוצות עם accent stripe, parent+PID, count+MiB, כפתור `✕ נקה הכל`
- [x] כפתור גלובלי `🧹 נקה את הכל` עם `messagebox.askyesno`
- [x] רענון הסקירה אחרי הריגה (`_refresh()`)
- [x] כפתור "רענן" ידני
- [x] הודעת "✓ אין תהליכים מיותרים" כשהרשימה ריקה

**בדיקה:** ✅ imports OK

### ✅ שלב 5 — `dock_strip.py` (אינדיקטור)
- [x] `Label` חדש בשורה הראשונה, צבע אמבר `#ebcb8b`, מודגש
- [x] עדכון בכל tick של הדוק (`janitor.count_total_zombies()`)
- [x] `pack_forget` כשהמספר 0, `pack(...before=lbl_cpu)` כשמופיע
- [x] `<Button-1>` → `open_cleanup_panel(root)`
- [x] הוסף גם פריט תפריט "🧹 ניקוי תהליכים מיותרים" בקליק-ימני

**בדיקה:** ✅ imports OK, 34/34 tests pass

### ✅ שלב 6 — `monitor.py` (אתחול)
- [x] אתחול `get_default_janitor()` ב-`main()` אם `enabled` ב-config וב-CLI
- [x] flag CLI `--janitor` / `--no-janitor` (BooleanOptionalAction)
- [x] תרגום `scan_interval_minutes` ל-seconds, העברת `conhost_threshold`

**בדיקה:** ✅ `python monitor.py --once` רץ נקי

### ✅ שלב 7 — בדיקה ידנית end-to-end
- [x] סריקה של המערכת האמיתית **מצאה 23 conhost זומבים** של `claude.exe` (PID 19280, ~168 MiB)
- [x] המוניטור הופעל מחדש (PID 24584) — האינדיקטור 🧹 23 אמור להופיע בדוק
- [ ] בדיקה אינטראקטיבית של החלון/הריגה — יבוצע על ידי המשתמש

### ☐ שלב 8 — commit סופי
- [ ] commit עם הודעה תיאורית

---

## בעיות שעלולות לצוץ
1. **`proc.parent()` של conhost יחזיר None** — תופסים ב-try/except, מתעלמים מאותו conhost
2. **`RotatingFileHandler` יוצר קבצים בנפרד מ-`monitor.log`** — אבל זה רצוי (audit נפרד)
3. **`tk.PhotoImage` לאמוג'י 🧹** — נשתמש ב-Unicode בלבד דרך font, בלי תמונה

## סטטוס נוכחי: שלב 0 הושלם, מתחיל בשלב 1
