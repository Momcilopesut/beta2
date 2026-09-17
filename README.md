# Discipline

A minimal life-coach assistant for one habit: plan tomorrow tonight, then do it.

## How it works

- **Plan Tomorrow** — before bed, write down the tasks you're committing to for the next day.
- **Today** — check tasks off as you complete them. Tasks only appear here once their day arrives, so you can't add to today and skip the planning step.
- **Streak** — counts consecutive days where every planned task was completed, encouraging you to keep planning realistic days.

## Running it

No install, no server. Open `index.html` in a browser.

All data is stored locally in your browser's `localStorage` — nothing leaves your computer. Because of that, data is tied to the specific browser/profile you use it in; use the same one each time.

## Files

- `index.html` — structure
- `style.css` — minimalist black/white (light and dark mode) styling
- `app.js` — all logic: storage, task management, streak calculation
