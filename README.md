# Face Sorter 📸

**[العربي](#العربي) · [English](#english)**

---

## العربي

مكتبة لأشكال الناس **بتتنقل معاك على فلاشة وبتكبر** مع كل جهاز. برنامج مجاني ومفتوح المصدر (رخصة MIT).

**كل الشغل بيحصل على الجهاز نفسه، ومفيش ولا صورة ولا بيانات بتترفع على النت.**

### التشغيل من الفلاشة (من غير تسطيب)
1. نزّل `FaceSorter_vX.Y_windows.zip` من صفحة **Releases**.
2. فك الملف، وانسخ الفولدرين **`FaceSorter`** و**`Library`** على الفلاشة جنب بعض.
3. على أي جهاز ويندوز 10 أو 11: دوس مرتين على **`FaceSorter\FaceSorter.bat`**.

**لما تنزّل نسخة جديدة:** استبدل فولدر `FaceSorter` بس، وسيب `Library` زي ما هو.

### الاستخدام
- **اللغة:** البرنامج بالعربي، وتقدر تغيّره لإنجليزي من زرار **🌐 English** اللي فوق.
الصفحات في القائمة الجانبية (Ctrl+1 لحد Ctrl+5):
- **🖥 الجهاز ده:** تختار فولدر الصور والوضع (تعلم، أو تعلم + ترتيب، أو ترتيب بس)، وبعدين تراجع وتحفظ. صور الآيفون (HEIC) بتتقري كمان.
- **🔍 مين ده؟:** تسحب صورة وتفلتها، فتعرف مين فيها، وتقدر تحفظ أي وش لاسم.
- **🔎 دوّر:** تختار شخص أو أكتر، فيطلعلك كل الصور اللي على الجهاز وهما فيها مع بعض.
- **👥 الناس:** كل اللي المكتبة تعرفهم. تقدر تسمّي، أو تدمج، أو تعزل، أو تمسح، أو تصدّر، أو تصلّح الوشوش.
- **📚 المكتبة:** الملف (نسخة، ونقل، وفتح مكتبة تانية)، و🔒 الباسورد، والمساحة و**نضّف**، والجلسات والتراجع، والسجل، والنسخ الاحتياطية.

### الخصوصية والمسؤولية
- المكتبة فيها **بيانات وشوش وأسامي**، ودي بيانات حساسة.
  - **استخدم البرنامج على صور ليك حق فيها، أو أصحابها موافقين.**
  - وخد بالك إن قوانين بلاد كتير بتنظّم بيانات الوش.
- لو المكتبة على فلاشة، **حط لها باسورد** من بطاقة المكتبة.
  - **احفظ مفتاح الاسترجاع في مكان آمن.**
  - من غير الباسورد ومن غير المفتاح، المكتبة متتفتحش أبداً.

---

## English

A **portable library of people's faces** that lives on a USB stick and **grows on every computer**.
Free and open source (MIT license).

**Everything runs on the computer itself; no photo or data is ever uploaded.**

### Run from a USB stick (nothing to install)
1. Download `FaceSorter_vX.Y_windows.zip` from **Releases**.
2. Unzip it and copy both folders, **`FaceSorter`** and **`Library`**, side by side onto the USB stick.
3. On any Windows 10/11 PC: double-click **`FaceSorter\FaceSorter.bat`**.

**Updating:** replace only the `FaceSorter` folder; keep `Library`.

### Using it
- **Language:** the window is in Arabic; switch it to English with the **🌐 English** button at the top.
Pages in the side menu (Ctrl+1 … Ctrl+5):
- **🖥 This computer:** choose a photos folder and a mode (learn / learn + sort / sort only), then review and save. iPhone photos (HEIC) are read too.
- **🔍 Who is this?:** drop a photo to see who is in it; save any face for a name.
- **🔎 Find:** choose one person or more to get every photo on this computer with all of them in it.
- **👥 People:** everyone the library knows: name, join, isolate, delete, export, or correct faces.
- **📚 Library:** the file (copy, move, open another), 🔒 password, space and **Clean**, sessions and undo, log, backups.

### Privacy and responsibility
- The library holds **face data and names**, which are sensitive.
  - **Only use it on photos you have the right to use, or whose owners agree.**
  - Many countries regulate face data.
- If the library is on a USB stick, **set a password** in the library card.
  - **Keep the recovery key somewhere safe.**
  - Without the password and the recovery key the library cannot be opened.

### Build / develop
- `python face_sorter.py` opens the window. `python face_sorter.py --help` lists the command-line tools.
- `python face_sorter.py accuracy <folder>` measures recognition on your own photos (one folder per person).
- `python -m unittest discover -s tests` runs the library tests.
- GitHub Actions builds the portable Windows zip and tests it on a clean Windows machine (`.github/workflows/build.yml`).
- Third-party licenses: `THIRD_PARTY_LICENSES.md`.
