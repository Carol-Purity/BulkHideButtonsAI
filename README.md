# DemoHider – Beginner's Guide

Hide buttons from the command bars of Dynamics 365 / model-driven apps **in bulk**,
across several tables at once, and bring them back with one command.

Built for demos: make the app show only the buttons that fit a customer's process.

---

## Contents

1. [How it works (2-minute overview)](#1-how-it-works)
2. [What you need](#2-what-you-need)
3. [One-time setup on your computer](#3-one-time-setup-on-your-computer)
4. [Connect to an environment (sign in)](#4-connect-to-an-environment)
5. [Build the button catalogue](#5-build-the-button-catalogue)
6. [Create the DemoHider solution](#6-create-the-demohider-solution)
7. [Write a preset (the JSON file)](#7-write-a-preset)
8. [Hide buttons](#8-hide-buttons)
9. [Bring buttons back (unhide)](#9-bring-buttons-back)
10. [Working with another environment or tenant](#10-another-environment-or-tenant)
11. [Troubleshooting](#11-troubleshooting)
12. [Command cheat sheet](#12-command-cheat-sheet)

---

## 1. How it works

Dynamics stores every command bar as XML (the "ribbon"). To hide a button you add
one line to it that says *"hide this button"*. DemoHider writes those lines for you.

```
 1. CATALOGUE     A list of every button in your environment, with exact IDs
                  (built once per environment)
 2. PRESET        A small file YOU write: which buttons, on which tables, where
 3. RUN           One command: checks everything, shows you the list, asks Y/N,
                  updates a solution called "DemoHider" and imports it
 4. RESULT        Press Ctrl+F5 in the app - the buttons are gone
 5. UNDO          One command brings them back
```

**Good to know**

- Nothing changes until you answer **Y**.
- Every change is backed up and can be undone.
- DemoHider only ever removes hides **it** created – never anyone else's customizations.
- A button hidden on a table is hidden in **every app** that uses that table in that environment.
- Hiding is **not security** – a user could still reach the action another way.
  Use security roles for anything that must truly be blocked.
- Use it in **demo / dev environments**, not in a customer's production.

---

## 2. What you need

| Item | Notes |
|---|---|
| Windows PC | The commands below are for Windows PowerShell |
| A Dynamics 365 / Power Apps environment | e.g. a Sales trial or sandbox |
| Security role **System Administrator** or **System Customizer** | in that environment |
| The DemoHider files | the `DemoHider` folder you were given |
| About 1 hour | for the one-time setup |

---

## 3. One-time setup on your computer

### 3.1 Install four tools

Install each with the default options.

| Tool | Where to get it | Important |
|---|---|---|
| **Visual Studio Code** | code.visualstudio.com | Your editor and terminal |
| **Python 3.10 or newer** | python.org → Downloads | On the first install screen **tick "Add Python to PATH"** |
| **Power Platform Tools** (gives you `pac`) | In VS Code: Extensions (Ctrl+Shift+X) → search **Power Platform Tools** → Install | |
| **Azure CLI** (gives you `az`) | Search "Install Azure CLI Windows" → Microsoft's MSI installer | Free. Used only to sign in – **no Azure subscription or cost** |

### 3.2 Check the tools

Close and reopen VS Code, open a terminal (**Ctrl+`**), and run:

```powershell
python --version
pac
az --version
```

Each should print a version or help text. If you see *"is not recognized"*,
restart your computer and try again; if it still fails, reinstall that tool.

### 3.3 Put the DemoHider folder in place

Copy the `DemoHider` folder anywhere, for example `C:\DemoHider`. It should look like this:

```
DemoHider\
  demohider.ps1          <- the command you run
  README.md              <- this guide
  QUICK-REFERENCE.md     <- one-page command summary
  scripts\
    build_catalog.py
    make_plan.py
    hide_engine.py
    publish_tables.py
  presets\
    sales-demo.json      <- an example preset
```

The folders `catalog`, `solutions`, `plans`, `runs` and `manifests` are created as you go.

In VS Code: **File → Open Folder → DemoHider**. Then open a terminal (**Ctrl+`**).
**Run every command in this guide from that terminal.** The prompt should end in `\DemoHider>`.

### 3.4 About running the script

Windows blocks downloaded scripts by default. So every DemoHider command in this guide starts with:

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1
```

This allows the script **for that one run only**. It does not change your computer's settings.

---

## 4. Connect to an environment

### 4.1 Find your details

You need three values. Write them down – you will use them in several commands.

| Placeholder | What it is | Example | Where to find it |
|---|---|---|---|
| `<EnvURL>` | Environment URL | `https://contoso.crm4.dynamics.com` | Open your app; copy the start of the browser address up to `.com` |
| `<Tenant>` | Your organisation's domain | `contoso.onmicrosoft.com` or `contoso.com` | The part after `@` in your work email usually works |
| `<EnvName>` | A short nickname **you** choose | `CUS-Dev` | Letters, numbers and dashes, no spaces |

> In all commands below, replace the `<...>` placeholders **including the `< >`**.
> Example: `--env-name <EnvName>` becomes `--env-name CUS-Dev`.

### 4.2 Sign in (two sign-ins, both open a browser window)

```powershell
pac auth create --name <EnvName> --environment <EnvURL>
az login --tenant <Tenant> --allow-no-subscriptions
```

Example:

```powershell
pac auth create --name CUS-Dev --environment https://contoso.crm4.dynamics.com
az login --tenant contoso.onmicrosoft.com --allow-no-subscriptions
```

`--allow-no-subscriptions` is needed because demo tenants normally have no Azure subscription.

### 4.3 Check the sign-in

```powershell
pac org who
az account get-access-token --resource <EnvURL> --query expiresOn
```

- The first shows your environment and user name.
- The second shows a date and time. ✅ You're connected.

> **Sign-ins expire** after about an hour of inactivity. If a later command says it could not
> get a token, just run the `az login ...` line again.

---

## 5. Build the button catalogue

The catalogue is a list of every button on the tables you choose, with their exact IDs.
Build it **once per environment**, and again after installing new apps or solutions.

### 5.1 Run it

```powershell
python scripts\build_catalog.py --env-url <EnvURL> --env-name <EnvName>
```

This reads **Account, Contact, Lead, Opportunity** plus the global buttons. It takes 1–5 minutes.

To choose your own tables, add `--tables` with the tables' **logical names**, separated by commas, no spaces:

```powershell
python scripts\build_catalog.py --env-url <EnvURL> --env-name <EnvName> --tables account,contact,lead,opportunity,quote,salesorder,incident
```

> **Logical names** are the internal names: Order = `salesorder`, Case = `incident`,
> custom tables include a prefix such as `cr4a1_project`.
> Find them in make.powerapps.com → **Tables** → open the table → **Properties**.

Running it again with more tables **adds** them; the existing ones are kept.

### 5.2 What success looks like

```
  Reading account ... 343 buttons
  Reading contact ... 364 buttons
  ...
Table-level buttons: 1361   Application-level buttons: 569
```

The files are saved in `catalog\<EnvName>\`. You never need to edit them.

### 5.3 If a table fails

```powershell
python scripts\build_catalog.py --env-url <EnvURL> --env-name <EnvName> --retry-failed
```

This retries **only** the tables that failed. If it timed out again, add `--split`
to fetch that table in smaller parts.

---

## 6. Create the DemoHider solution

DemoHider puts its hides into a small solution called **DemoHider**. You create it once per environment.

### 6.1 Create it in the browser

1. Go to **make.powerapps.com** and pick your environment (top right).
2. **Solutions → New solution**
   - Display name: `DemoHider`  (the name field fills in as `DemoHider`)
   - Publisher: choose **your own publisher** (not "Default Publisher")
   - **Create**
3. Open the solution → **Add existing → Table** → tick **every table you want to hide buttons on**
   (e.g. Account, Contact, Lead, Opportunity) → **Next**.
4. On the next screen **do not select any components** and leave
   *Include all objects* and *Include table metadata* **unticked** → **Add**.

The solution stays tiny, so imports are fast.

### 6.2 Download it to your computer (export + unpack)

```powershell
pac solution export --name DemoHider --path solutions\DemoHider.zip
pac solution unpack --zipfile solutions\DemoHider.zip --folder solutions\DemoHider --packagetype Unmanaged
```

**Check:** the folder `solutions\DemoHider\Entities` now contains one folder per table.

### 6.3 Adding tables later

1. Add the table to the DemoHider solution in the browser (same as 6.1 step 3–4).
2. Delete (or rename) the folder `solutions\DemoHider` on your computer.
3. Run the two commands from 6.2 again.
4. Make sure the table is also in the catalogue (5.1 with `--tables`).

> Do **not** delete the `manifests` or `runs` folders – they remember what is hidden.

---

## 7. Write a preset

A **preset** is a small text file in the `presets` folder that says **what to hide**.
You write it once and reuse it in every demo and every environment.

### 7.1 The idea: short button names

Every button has a **short name** that is the **same on every table**:

| On the screen | Short name |
|---|---|
| Run Report (on lists/subgrids) | `RunReport` |
| Run Report (on forms) | `Reports` |
| Word Templates | `WordTemplate` |
| Excel Templates | `DocumentTemplate` |
| Flow | `Flows.RefreshCommandBar` |
| Email a Link (on lists) | `Send` |
| Visualize this view | `MainTab.QuickPowerBI.Button` |
| Quick Campaign | `QuickCampaign` and `ModernQuickCampaign` |

So *"hide RunReport on account, contact and lead"* hides it on all three tables at once.

### 7.2 Find the short name of any button

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -Find <word>
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -Find <word> -Table <table>
```

Example:

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -Find excel -Table contact
```

```
BUTTON (use this in presets)     NAME                       WHERE         TABLES
ExportSelectedToExcel            Export Selected To Excel   list          contact
ExportToExcel                    Export To Excel            list          contact
ImportDataFromExcel              Import From Excel          list          contact
```

- Use the value in the **BUTTON** column in your preset.
- **WHERE** tells you where it appears: `list` (the main list of records), `form`
  (inside a record), `subgrid` (a list shown inside a form).
- Rows marked `[application]` are global buttons – see 7.6.
- Search with a word from the button's name on screen (e.g. `report`, `template`, `flow`, `share`).
  If nothing is found, try a shorter word.

### 7.3 Create your preset file

1. In VS Code, right-click the `presets` folder → **New File** → name it, e.g. `my-demo.json`
   (lowercase, no spaces, ends in `.json`).
2. Paste this template and change the parts in **CAPITALS**:

```json
{
  "name": "my-demo",
  "description": "WHAT THIS PRESET IS FOR",
  "rules": [
    {
      "description": "WHY THESE BUTTONS ARE HIDDEN",
      "tables": ["account", "contact", "lead", "opportunity"],
      "where": ["list", "form", "subgrid"],
      "buttons": ["RunReport", "Reports", "WordTemplate", "DocumentTemplate"]
    }
  ],
  "keep": []
}
```

3. Save (**Ctrl+S**).

### 7.4 What each line means

| Line | Meaning | Rules |
|---|---|---|
| `"name"` | The preset's name | Same as the file name, without `.json` |
| `"description"` | A note for you | Any text |
| `"rules"` | A list of rules. A button is hidden if **any** rule matches | Each rule is inside `{ }` |
| `"description"` (in a rule) | Why – shown in the preview | Any text |
| `"tables"` | Which tables | Logical names in lowercase, or `"*"` for every table in the catalogue |
| `"where"` | Which places | Any of `"list"`, `"form"`, `"subgrid"`. Leave the line out for all three |
| `"buttons"` | Which buttons | Short names from `-Find` |
| `"keep"` | Exceptions that are **never** hidden | Written like a rule; `[]` means none |

### 7.5 Examples

**Hide the same buttons on several tables at once** – list the tables:

```json
{
  "description": "No reports on the main sales tables",
  "tables": ["account", "contact", "lead", "opportunity"],
  "buttons": ["RunReport", "Reports"]
}
```

**On every table in the catalogue** – use `"*"`:

```json
{
  "description": "No Word templates anywhere",
  "tables": "*",
  "buttons": ["WordTemplate"]
}
```

**Only in one place** – add `"where"`:

```json
{
  "description": "Email a Link only clutters the lists",
  "tables": ["account", "contact"],
  "where": ["list"],
  "buttons": ["Send"]
}
```

**Different buttons for different tables** – use two rules:

```json
"rules": [
  {
    "description": "Marketing buttons",
    "tables": ["account", "contact", "lead"],
    "buttons": ["QuickCampaign", "ModernQuickCampaign"]
  },
  {
    "description": "Follow buttons on opportunities",
    "tables": ["opportunity"],
    "buttons": ["FollowButton", "UnfollowButton", "NewUnfollowButton"]
  }
]
```

**Hide everything that starts with a prefix** (e.g. all Power Pages buttons):

```json
{
  "description": "Power Pages portal buttons",
  "tables": "*",
  "id_starts_with": ["adx.", "Microsoft.PowerPages."]
}
```

**Exceptions** – hide Run Report everywhere except on Opportunity:

```json
"rules": [
  { "description": "No reports", "tables": "*", "buttons": ["RunReport", "Reports"] }
],
"keep": [
  { "tables": ["opportunity"], "buttons": ["RunReport", "Reports"] }
]
```

Other conditions you can use in a rule instead of (or with) `"buttons"`:

| Condition | Matches buttons whose… | Example |
|---|---|---|
| `"id_starts_with"` | internal ID starts with the text | `["adx."]` |
| `"id_contains"` | internal ID contains the text | `["Excel"]` |
| `"label_contains"` | name contains the text | `["Template"]` |
| `"commands"` | action is one of these | `["Mscrm.ExportToExcel"]` |

Inside one rule, **all** conditions must match.

### 7.6 Global buttons (advanced, optional)

Rules normally hide a button **table by table**. With `"scope": "application"` a rule hides
a global button on **all tables at once**:

```json
{
  "description": "Email a Link on every list",
  "scope": "application",
  "where": ["list"],
  "buttons": ["Send"]
}
```

This needs the **Application Ribbon** in the DemoHider solution:
make.powerapps.com → DemoHider → **Switch to classic** → **Add Existing → Client Extensions →
Application Ribbons**, then export and unpack again (6.3 steps 2–3).
Use the names `-Find` shows with `[application]`.

### 7.7 JSON mistakes to avoid

JSON is strict. Most errors are one of these:

| ✅ Right | ❌ Wrong |
|---|---|
| `"tables": ["account", "contact"]` | `"tables": [account, contact]` (missing quotes) |
| A comma **between** items: `"a", "b"` | A comma **after the last** item: `"a", "b",` |
| Straight quotes `"` | Curly quotes `“ ”` (from Word or email) |
| `"contact"` (lowercase logical name) | `"Contacts"` (display name) |

VS Code underlines JSON errors in red – fix them before running.
If DemoHider says *"is not valid JSON"*, it also tells you the line number.

### 7.8 The example preset

`presets\sales-demo.json` is a ready-made preset. On Account, Contact, Lead and Opportunity it hides
reports, templates, flows, workflows, mail merge, Email a Link, Visualize this view, quick campaigns,
Follow/Unfollow and Power Pages buttons. Copy it as a starting point for your own.

---

## 8. Hide buttons

### 8.1 The command

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -Preset presets\<preset-file>.json
```

Example:

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName CUS-Dev -Preset presets\sales-demo.json
```

> `-EnvName` picks the catalogue (`catalog\<EnvName>`). If you always use the same environment
> you can make it the default: open `demohider.ps1`, find the line
> `[string]$EnvName = "CUS-Dev",` near the top and change `CUS-Dev` to your `<EnvName>`.
> Then you can leave `-EnvName` out.

### 8.2 What happens

```
==> Building the list from preset 'sales-demo'
135 button(s) to hide:
  account   form     9
  account   list     13
  ...
==> Step 1 of 4 - Checking (nothing is changed yet)

What will happen:
  HIDE     account      Form          Run Report (Reports)
  HIDE     contact      HomepageGrid  Word Template
  ... and 95 more
  Full details: ...\runs\20261026-101500-sales-demo\preview.md

Apply these 135 change(s) to Dynamics? (Y/N):
```

- **HomepageGrid** = the main list, **Form** = inside a record, **SubGrid** = a list inside a form.
- For the full list, open the `preview.md` it names (in VS Code: right-click → **Open Preview**).
- Type **Y** and press **Enter** to continue, or **N** to cancel (nothing is changed).

It then updates the files, runs a safety check, imports into Dynamics and publishes the
changed tables. This takes **2–10 minutes**. Wait for:

```
Done! In the app press Ctrl+F5 to see the result.
```

### 8.3 Check the result

Open the app, go to the table, press **Ctrl+F5** (a normal refresh may still show the old buttons).
Check the list, a record (form) and the **⋮** overflow menu.

### 8.4 Running it again is safe

Running the same preset again shows *"Nothing to change"*. You can also add more rules to the
preset and run it again – only the new buttons are hidden.

---

## 9. Bring buttons back

| You want to… | Command |
|---|---|
| Bring back the buttons of **one preset** | `powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -Preset presets\<preset-file>.json -Unhide` |
| Undo **only the last** change | `powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -Undo` |
| Bring back **everything** DemoHider has hidden (reset after a demo) | `powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -UnhideAll` |

Each shows what will come back and asks **Y/N**, like hiding.
Then press **Ctrl+F5** in the app.

**To show just one or two buttons again:** remove them from the preset, save it, and use
`-Unhide` with a small preset containing only those buttons – or simply `-UnhideAll`
and then run your edited preset again.

> `-Undo` only reverses the most recent change. When in doubt, use **`-UnhideAll`**.

---

## 10. Another environment or tenant

Presets work in every environment – only the catalogue and the solution are per environment.

1. **Sign in** (section 4) with a new `<EnvName>`:
   ```powershell
   pac auth create --name <EnvName> --environment <EnvURL>
   az login --tenant <Tenant> --allow-no-subscriptions
   ```
2. **Build its catalogue** (section 5):
   ```powershell
   python scripts\build_catalog.py --env-url <EnvURL> --env-name <EnvName>
   ```
3. **Create a DemoHider solution there** (6.1), then export and unpack it to its **own folder**:
   ```powershell
   pac solution export --name DemoHider --path solutions\DemoHider-<EnvName>.zip
   pac solution unpack --zipfile solutions\DemoHider-<EnvName>.zip --folder solutions\DemoHider-<EnvName> --packagetype Unmanaged
   ```
4. **Run with both `-EnvName` and `-Solution`:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -Solution solutions\DemoHider-<EnvName> -Preset presets\sales-demo.json
   ```
   Use the same two options for `-Unhide`, `-Undo` and `-UnhideAll`.

**Switching between environments:** `pac` imports into the environment you are currently signed in to.

```powershell
pac auth list                      # shows your connections; * marks the active one
pac auth select --name <EnvName>   # switch
pac org who                        # confirm
```

> ⚠️ Always run `pac org who` before hiding, so you change the environment you intend to.
> For another **tenant**, also run `az login --tenant <Tenant> --allow-no-subscriptions` again.

---

## 11. Troubleshooting

| Message or problem | What to do |
|---|---|
| `... is not recognized as the name of a cmdlet` | The tool isn't installed or VS Code was open during install. Restart VS Code (or the PC). |
| `running scripts is disabled on this system` | Start the command with `powershell -ExecutionPolicy Bypass -File .\demohider.ps1` (3.4). |
| `Could not get a token` | Sign-in expired: `az login --tenant <Tenant> --allow-no-subscriptions` |
| `Catalogue not found: catalog\...` | Build the catalogue (section 5) or check `-EnvName` matches the folder name in `catalog`. |
| `Solution folder not found` | Export and unpack (6.2), or check `-Solution`. |
| `is not valid JSON` | Fix the preset (7.7). The message gives the line number. |
| `NOTE: no match in this environment for: X` | That button name doesn't exist on those tables here. Check it with `-Find`. Other rules still work. |
| `BLOCKED ... table 'x' is not in the solution` | Add the table to DemoHider and export/unpack again (6.3). |
| `BLOCKED ... not found in catalogue` / `not found in the source ribbon XML` | The catalogue is out of date: rebuild it (5.1). |
| `BLOCKED ... Hidden by another customization` | Someone else hid it (e.g. with Ribbon Workbench). DemoHider leaves it alone. |
| `changed since the preview` | Run the same command again. |
| A table times out while building the catalogue | `--retry-failed`, and add `--split` if needed (5.3). |
| Import or publish drops the connection | The import usually still finished. Wait a few minutes and press Ctrl+F5. If nothing changed: `python scripts\publish_tables.py --run runs\<newest-run-folder>` |
| Buttons still visible after Done | Press **Ctrl+F5**, or open the app in a private window. Check that you are in the right environment (`pac org who`). |
| One particular button won't hide | It may be a different button with a similar name (e.g. form vs list – check `-Find`), or a **modern command** (e.g. *Open Dashboards*), which DemoHider cannot hide. |

When asking for help, copy the **whole** terminal output.

---

## 12. Command cheat sheet

Replace `<...>` with your values. `DH` below is short for
`powershell -ExecutionPolicy Bypass -File .\demohider.ps1`.

| Task | Command |
|---|---|
| Sign in | `pac auth create --name <EnvName> --environment <EnvURL>` and `az login --tenant <Tenant> --allow-no-subscriptions` |
| Check sign-in | `pac org who` |
| Switch environment | `pac auth select --name <EnvName>` |
| Build catalogue | `python scripts\build_catalog.py --env-url <EnvURL> --env-name <EnvName>` |
| Add tables to catalogue | `... --tables account,contact,quote` |
| Retry failed tables | `... --retry-failed` (add `--split` if needed) |
| Export + unpack solution | `pac solution export --name DemoHider --path solutions\DemoHider.zip` then `pac solution unpack --zipfile solutions\DemoHider.zip --folder solutions\DemoHider --packagetype Unmanaged` |
| Find a button's short name | `DH -EnvName <EnvName> -Find <word>` (add `-Table <table>`) |
| Hide with a preset | `DH -EnvName <EnvName> -Preset presets\<file>.json` |
| Unhide a preset | `DH -EnvName <EnvName> -Preset presets\<file>.json -Unhide` |
| Undo last change | `DH -EnvName <EnvName> -Undo` |
| Bring back everything | `DH -EnvName <EnvName> -UnhideAll` |
| Other environment | add `-Solution solutions\DemoHider-<EnvName>` to every `DH` command |

### A typical demo, start to finish

```powershell
pac org who                                                                          # 1. right environment?
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName CUS-Dev -Preset presets\sales-demo.json   # 2. hide, answer Y
#    ...Ctrl+F5, run the demo...
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName CUS-Dev -UnhideAll                        # 3. reset, answer Y
```

---

### Appendix – picking buttons in Excel (optional)

Instead of a preset you can tick buttons in a spreadsheet:

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -NewPicker -Table <table>
```

Open the file it names in `pickers\` with Excel, type `x` in the **hide** column for each button,
save as CSV, then:

```powershell
powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -EnvName <EnvName> -Picker pickers\<file>.csv
```

Add `-Unhide` to the same command to bring those buttons back.
