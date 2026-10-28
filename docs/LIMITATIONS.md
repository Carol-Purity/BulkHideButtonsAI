# DemoHider – Limitations

Read this before using DemoHider, and before sharing it with a customer or colleague.
DemoHider is a convenience tool for **demo and development environments**.

---

## 1. What it can and cannot hide

| Can hide | Cannot hide |
|---|---|
| Classic command bar buttons defined in the ribbon (`RibbonDiffXml`) – the large majority of out-of-the-box buttons on lists, forms and subgrids | **Modern commands** built with the command designer / Power Fx (e.g. *Open Dashboards* in some apps). These are stored differently and do not appear in the catalogue. Hide them in the command designer instead. |
| Buttons added by installed apps and ISV solutions (Power Pages, Sales sequences, Field Service, 3CX, etc.) | Buttons in the **site map**, the app header (search, Copilot, settings, help), form tabs, or the timeline |
| Menus (hiding a menu hides everything in it) | Individual entries inside **dynamic menus** that are filled at runtime (e.g. the list of flows under *Flow*, the list of reports under *Run Report*, the list of templates) – you can only hide the whole menu |
| Global buttons via the Application Ribbon (advanced, preset route only) | Global buttons from the tick-box page (it shows table-level buttons only) |

## 2. Scope of a hide

- **Per table, per environment – not per app.** A button hidden on Contact is hidden on Contact in
  **every** model-driven app in that environment that uses it.
- **For everyone.** Hides are not per user or per security role. Showing a button only to some users
  needs a custom enable/display rule (or a Power Fx visibility formula on a modern command) – not
  something DemoHider does.
- **Hiding is not security.** A hidden action can still be performed another way (another button,
  bulk edit, Advanced Find, Excel, the API, Power Automate). Use **security roles** for anything that
  must really be blocked.

## 3. What you see vs. what is listed

- The catalogue lists **every button defined** for a table. Many are never on screen at the same time:
  Dynamics shows or hides each one live, based on your security role, the record's state, whether a
  record is selected, feature switches, and the device.
- DemoHider flags a button as **"not in modern apps"** only when its rules make that certain
  (e.g. `Mscrm.HideOnModern`, `Mscrm.HideOnCommandBar`, Outlook-only, never-enabled). Other buttons
  may still be invisible for a particular user for reasons that can only be known in the browser.
- Buttons that **someone else already hid** (Ribbon Workbench, another solution) are not returned by
  Dynamics, so they are not in the catalogue and DemoHider cannot show or restore them.
- Button names come from the ribbon. Microsoft's built-in labels are not available through the API,
  so some names are best guesses (the internal short name is always shown underneath).
  Labels assume English (language code 1033).

## 4. How changes are made

- DemoHider writes `HideCustomAction` entries into an **unmanaged** solution called *DemoHider* and
  imports it. This is a standard, supported way to customize the ribbon, but:
  - It only touches hides **it created** (tagged `demohider.`). It never removes anyone else's.
  - Other layers can still win. A managed solution installed later, or another customization, can
    add or re-show buttons.
  - Each run imports the **tables it changed**, with everything inside them (a trimmed copy of the
    solution). If those tables include forms, views or columns, they are re-imported in the state they
    had when you exported, which can overwrite later changes. Keep the tables in DemoHider bare
    (no subcomponents). With `-FullSolution`, or when DemoHider falls back to the whole solution,
    **everything** in it is re-imported.
  - If someone changes the DemoHider solution directly in Dynamics, export and unpack it again
    before the next run, or their change can be overwritten.
- **Undo / rollback** restores the local solution files; Dynamics only changes when the solution is
  imported again (the runner does this for you).
- The catalogue is a **snapshot**. After installing or updating apps, or after Microsoft updates,
  rebuild it – button IDs can be added, renamed or removed.
- Tables must be **customizable** and included in the DemoHider solution.

## 5. Practical limits

- **Windows only** for the one-command runner (`demohider.ps1`). The Python scripts would run
  elsewhere, but the runner is PowerShell with Windows paths.
- Requires **Python 3.10+**, the **Power Platform CLI (`pac`)** and the **Azure CLI**, plus the
  System Administrator or System Customizer role. Some organizations block Azure CLI sign-in by policy.
- Reading large ribbons (e.g. Account) can take minutes. Import and publish take 2–10 minutes and the
  connection can drop on busy environments (the import usually still completes).
- Browsers **cache** the command bar: press **Ctrl+F5** or use a private window after each change.
- The tick-box, confirmation and progress pages run a small local web server on `127.0.0.1` while they
  are open; it is not reachable from other machines. Keep the terminal window open while they run.
- Tested in one environment/tenant so far. Other tenants should behave the same, but report anything
  unexpected.

## 6. Use responsibly

- Use DemoHider in **demo, trial and development environments**. Do not use it in a customer's
  production environment without their agreement and your normal change process.
- Always check the environment the runner shows before answering **Y**.
- DemoHider is provided **as is**, without warranty (see [LICENSE](../LICENSE)).
- DemoHider is an independent project. It is **not affiliated with, endorsed by, or supported by
  Microsoft**. Microsoft, Dynamics 365, Power Apps, Power Platform and Dataverse are trademarks of the
  Microsoft group of companies.
