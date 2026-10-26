# DemoHider – quick guide

Hide command bar buttons in Dynamics 365 / model-driven apps in bulk, and bring them back.
Run every command in the VS Code terminal from the DemoHider folder.

If Windows blocks the script, start each command with
`powershell -ExecutionPolicy Bypass -File` instead of `.\`, e.g.
`powershell -ExecutionPolicy Bypass -File .\demohider.ps1 -UnhideAll`

## Folder layout

| Folder | Contents |
|---|---|
| `scripts\` | build_catalog.py, make_plan.py, hide_engine.py, publish_tables.py |
| `presets\` | Reusable rule files, e.g. sales-demo.json |
| `pickers\` | Spreadsheets for ticking buttons by hand |
| `catalog\<env>\` | Every button in an environment (rebuild per environment) |
| `solutions\DemoHider\` | The unpacked DemoHider solution |
| `plans\`, `runs\`, `manifests\` | Created automatically – what was asked, what was done, what is hidden |

## Everyday commands

| I want to… | Command |
|---|---|
| Hide a preset | `.\demohider.ps1 -Preset presets\sales-demo.json` |
| Bring a preset back | `.\demohider.ps1 -Preset presets\sales-demo.json -Unhide` |
| Tick buttons by hand | `.\demohider.ps1 -NewPicker` → tick `x` in Excel → `.\demohider.ps1 -Picker pickers\CUS-Dev.csv` |
| Find a button's short name | `.\demohider.ps1 -Find report` (add `-Table contact`) |
| Undo the last change | `.\demohider.ps1 -Undo` |
| Bring back everything | `.\demohider.ps1 -UnhideAll` |

Every command shows the list first and asks **Y/N** before changing anything.
After it says **Done**, press **Ctrl+F5** in the app.

## Writing a preset

A preset is a list of rules. Each rule says **which buttons**, on **which tables**, in **which places**.

```json
{
  "name": "my-preset",
  "description": "What it is for",
  "rules": [
    {
      "description": "Reports and templates",
      "tables": ["account", "contact"],
      "where": ["list", "form", "subgrid"],
      "buttons": ["RunReport", "Reports", "WordTemplate"]
    }
  ],
  "keep": []
}
```

- **buttons** – short names from `-Find`. The same name works on every table.
- **tables** – logical names, or `"*"` for every table in the catalogue.
- **where** – `list`, `form`, `subgrid` (leave out for all three).
- **keep** – exceptions that are never hidden, written like a rule.
- Other conditions: `id_starts_with`, `id_contains`, `label_contains`, `commands`.
- `"scope": "application"` hides a button on **all** tables at once
  (the Application Ribbon must be in the DemoHider solution).

## New environment or tenant

1. `pac auth create --name <Name> --environment <url>` and `az login --tenant <tenant> --allow-no-subscriptions`
2. `python scripts\build_catalog.py --env-url <url> --env-name <Name>`
3. Create a DemoHider solution there with the tables you need, then
   `pac solution export --name DemoHider --path solutions\DemoHider-<Name>.zip` and
   `pac solution unpack --zipfile solutions\DemoHider-<Name>.zip --folder solutions\DemoHider-<Name> --packagetype Unmanaged`
4. `.\demohider.ps1 -EnvName <Name> -Solution solutions\DemoHider-<Name> -Preset presets\sales-demo.json`

## Limits

- Modern commands (made in the command designer, e.g. *Open Dashboards*) cannot be hidden this way.
- Hiding is not security – users may still reach the action another way.
- A button hidden on a table is hidden in every app that uses that table in the environment.
