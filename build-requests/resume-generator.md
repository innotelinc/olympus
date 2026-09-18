# Application Specification: Resume Generator

> Exported from Olympus Studio on 2026-09-17T21:57:56.135Z (project `YDX7WWn4vmMX`).
>
> Kind: **app**.

## ▶︎ Next steps

1. Manufacture it here: `make app SPEC=build-requests/resume-generator.md`
2. Or commit it (`git add build-requests/resume-generator.md`) — `.github/workflows/olympus-app-builder.yml` builds it on push.
3. Package and run it from its own plan: `python3 scripts/package-project.py resume-generator` then `python3 scripts/app-runtime.py --up resume-generator --build`
4. Or do the same from Studio, which needs no shell: **Preview It** runs it and frames it under `resume-generator-preview.<suffix>`, and **Publish It** puts it on `resume-generator.<suffix>` (see docs/site-publishing.md).
5. Do not run `package-app.py` or `package-website.py` for this project. They build a fixed stack and would replace what the plan already decided.

## 🎯 Core Purpose

The last build of this project failed. Fix it, keeping the confirmed plan and every feature that already works.
What the build reported: No such build job.
Return only the files that need to change, in full. Fix the cause of the failure, not the symptom — and do not remove a feature to make an error go away.

## 🧰 Tech Stack

- static — from the plan this project was built to, which is what it is packaged and run as
- Runs with `nginx -g 'daemon off;'` on port 3000, health checked at `/`
- HTML
- CSS
- JavaScript / TypeScript
- Config (JSON / YAML / TOML)

## 🛠️ Key Features & Pages

As built in Studio:

- **`index.html`** — entry point (4.9 KB)
- **`styles.css`** — styles (7.5 KB)
- **`app.js`** — behaviour (17.4 KB)
- **`Dockerfile`** — supporting file (124 B)
- **`nginx.conf`** — supporting file (252 B)
- **`.github/workflows/ci.yml`** — supporting file (279 B)

The client's entry point is `index.html`.

## 🚦 Verification Criteria

- the container built from `plan.json` starts with `nginx -g 'daemon off;'` and answers `/` on port 3000
- every screen the spec lists works against the API: with rows, with no rows, and while the request is in flight

## 🧱 Packaging & runtime

The stack is the plan's: static.
`scripts/package-project.py` generates the Dockerfile from that plan — the base image
from `runtime.language`, the plan's install and build commands run inside it — and
`scripts/app-runtime.py` runs the image on the plan's port, checking the plan's
healthcheck path. Nothing is installed on the host, and nothing else decides the stack.

```bash
python3 scripts/package-project.py resume-generator
python3 scripts/app-runtime.py --up resume-generator --build   # image + container
```

Write only the files the plan lists, into the working directory, and do not run a
packager: packaging is the step after this one, and the older packagers build a stack
of their own rather than the one this project was planned in.

## 📎 Reference build (from Studio)

The files below are the build this spec came from. Treat them as reference for
features and intent rather than a structure to preserve — the factory should build
the app the spec describes.

### `index.html`

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Resume &amp; Cover Letter Generator</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <header class="app-header">
    <div>
      <h1>Resume &amp; Cover Letter Generator</h1>
      <p>Fill in your details — your documents update live.</p>
    </div>
    <button class="btn btn-secondary" id="reset-btn" type="button">Reset to Sample</button>
  </header>

  <main class="app-layout">
    <!-- Form Panel -->
    <section class="form-panel" aria-label="Resume and cover letter form">
      <form id="resume-form" autocomplete="off">
        <div class="form-section">
          <h2>Personal Information</h2>
          <div class="form-row">
            <label for="personal-name">Full Name</label>
            <input type="text" id="personal-name" name="personal-name" value="Alex Johnson">
          </div>
          <div class="form-row">
            <label for="personal-title">Job Title</label>
            <input type="text" id="personal-title" name="personal-title" value="Frontend Developer">
          </div>
          <div class="form-row">
            <label for="personal-email">Email</label>
            <input type="email" id="personal-email" name="personal-email" value="alex.johnson@example.com">
          </div>
          <div class="form-row">
            <label for="personal-phone">Phone</label>
            <input type="tel" id="personal-phone" name="personal-phone" value="(555) 123-4567">
          </div>
          <div class="form-row">
            <label for="personal-location">Location</label>
            <input type="text" id="personal-location" name="personal-location" value="San Francisco, CA">
          </div>
          <div class="form-row">
            <label for="personal-website">Website / LinkedIn</label>
            <input type="text" id="personal-website" name="personal-website" value="linkedin.com/in/alexjohnson">
          </div>
        </div>

        <div class="form-section">
          <h2>Professional Summary</h2>
          <div class="form-row">
            <label for="summary">Summary</label>
            <textarea id="summary" name="summary" rows="4">Results-driven frontend developer with 5+ years of experience building accessible, responsive web applications. Passionate about clean code, user experience, and mentoring junior developers.</textarea>
          </div>
        </div>

        <div class="form-section">
          <h2>Experience</h2>
          <div id="experience-list"></div>
          <button type="button" class="btn btn-add" id="add-experience-btn">+ Add Experience</button>
        </div>

        <div class="form-section">
          <h2>Education</h2>
          <div id="education-list"></div>
          <button type="button" class="btn btn-add" id="add-education-btn">+ Add Education</button>
        </div>

        <div class="form-section">
          <h2>Skills</h2>
          <div class="form-row">
            <label for="skills">Skills (comma separated)</label>
            <textarea id="skills" name="skills" rows="3">JavaScript, React, CSS, Node.js, Accessibility, Git</textarea>
          </div>
        </div>

        <div class="form-section">
          <h2>Cover Letter Details</h2>
          <div class="form-row">
            <label for="cover-recipient">Recipient Name</label>
            <input type="text" id="cover-recipient" name="cover-recipient" value="Hiring Manager">
          </div>
          <div class="form-row">
            <label for="cover-company">Company</label>
            <input type="text" id="cover-company" name="cover-company" value="Tech Corp">
          </div>
          <div class="form-row">
            <label for="cover-position">Position</label>
            <input type="text" id="cover-position" name="cover-position" value="Senior Frontend Developer">
          </div>
          <div class="form-row">
            <label for="cover-notes">Additional Notes</label>
            <textarea id="cover-notes" name="cover-notes" rows="3">I am particularly excited about your focus on user experience and the opportunity to contribute to a collaborative team.</textarea>
          </div>
        </div>
      </form>
    </section>

    <!-- Preview Panel -->
    <section class="preview-panel" aria-label="Document preview">
      <div class="preview-toolbar">
        <div class="tabs" role="tablist">
          <button type="button" class="tab active" id="tab-resume" role="tab" aria-selected="true">Resume</button>
          <button type="button" class="tab" id="tab-cover" role="tab" aria-selected="false">Cover Letter</button>
        </div>
        <div class="preview-actions">
          <button type="button" class="btn btn-primary" id="print-btn">Print / Save as PDF</button>
        </div>
      </div>
      <div class="preview-scroll">
        <div id="preview-content"></div>
      </div>
    </section>
  </main>

  <script src="app.js"></script>
</body>
</html>
```

### `styles.css`

```css
:root {
  --color-bg: #f0f2f5;
  --color-surface: #ffffff;
  --color-border: #d9dee3;
  --color-text: #1a1d21;
  --color-muted: #6b7280;
  --color-primary: #2563eb;
  --color-primary-dark: #1d4ed8;
  --color-accent: #f59e0b;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.06);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.08);
  --radius: 8px;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --font-serif: Georgia, "Times New Roman", serif;
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: var(--font-sans);
  background: var(--color-bg);
  color: var(--color-text);
  line-height: 1.5;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 24px;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  box-shadow: var(--shadow-sm);
}

.app-header h1 {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.app-header p {
  color: var(--color-muted);
  font-size: 0.9rem;
}

.btn {
  font-family: inherit;
  font-size: 0.9rem;
  font-weight: 600;
  padding: 8px 16px;
  border-radius: var(--radius);
  border: 1px solid transparent;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.btn-primary {
  background: var(--color-primary);
  color: #fff;
}

.btn-primary:hover {
  background: var(--color-primary-dark);
}

.btn-secondary {
  background: var(--color-surface);
  border-color: var(--color-border);
  color: var(--color-text);
}

.btn-secondary:hover {
  background: #f8fafc;
  border-color: #cbd5e1;
}

.btn-add {
  background: transparent;
  border: 1px dashed var(--color-primary);
  color: var(--color-primary);
  width: 100%;
  padding: 10px;
  margin-top: 8px;
}

.btn-add:hover {
  background: #eff6ff;
}

.app-layout {
  display: grid;
  grid-template-columns: minmax(320px, 420px) 1fr;
  gap: 0;
  height: calc(100vh - 73px);
}

.form-panel {
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  overflow-y: auto;
  padding: 24px;
}

.form-section {
  margin-bottom: 28px;
  padding-bottom: 24px;
  border-bottom: 1px solid var(--color-border);
}

.form-section:last-child {
  border-bottom: none;
  margin-bottom: 0;
  padding-bottom: 0;
}

.form-section h2 {
  font-size: 1rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-primary);
  margin-bottom: 14px;
}

.form-row {
  margin-bottom: 12px;
}

.form-row label {
  display: block;
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--color-muted);
  margin-bottom: 4px;
}

.form-row input,
.form-row textarea {
  width: 100%;
  padding: 8px 10px;
  font-family: inherit;
  font-size: 0.9rem;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: #fff;
  color: var(--color-text);
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.form-row input:focus,
.form-row textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.15);
}

.form-row textarea {
  resize: vertical;
  min-height: 60px;
}

.experience-item,
.education-item {
  background: #f8fafc;
  border: 1px solid var(--color-border);
  border-radius: var(--radius);
  padding: 14px;
  margin-bottom: 12px;
  position: relative;
}

.experience-item .remove-btn,
.education-item .remove-btn {
  position: absolute;
  top: 8px;
  right: 8px;
  background: transparent;
  border: none;
  color: var(--color-muted);
  font-size: 1.2rem;
  line-height: 1;
  cursor: pointer;
  padding: 4px;
  border-radius: 4px;
}

.experience-item .remove-btn:hover,
.education-item .remove-btn:hover {
  color: #dc2626;
  background: #fee2e2;
}

.preview-panel {
  display: flex;
  flex-direction: column;
  background: #e5e7eb;
  overflow: hidden;
}

.preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}

.tabs {
  display: flex;
  gap: 4px;
}

.tab {
  font-family: inherit;
  font-size: 0.9rem;
  font-weight: 600;
  padding: 8px 16px;
  border: none;
  background: transparent;
  color: var(--color-muted);
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.tab:hover {
  background: #f1f5f9;
  color: var(--color-text);
}

.tab.active {
  background: var(--color-primary);
  color: #fff;
}

.preview-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 32px;
  display: flex;
  justify-content: center;
}

#preview-content {
  width: 100%;
  max-width: 800px;
  background: var(--color-surface);
  box-shadow: var(--shadow-md);
  border-radius: 4px;
  padding: 48px;
  min-height: 100%;
}

/* Document styles */
.document {
  font-family: var(--font-sans);
  color: #1a1d21;
}

.document h1 {
  font-size: 2rem;
  font-weight: 700;
  letter-spacing: -0.02em;
  margin-bottom: 2px;
}

.document .job-title {
  font-size: 1.1rem;
  color: var(--color-primary);
  font-weight: 600;
  margin-bottom: 8px;
}

.document .contact-line {
  font-size: 0.9rem;
  color: var(--color-muted);
  margin-bottom: 20px;
}

.document .contact-line span {
  margin-right: 12px;
}

.document h2 {
  font-size: 1rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  border-bottom: 2px solid var(--color-primary);
  padding-bottom: 4px;
  margin: 24px 0 12px;
}

.document .summary-text {
  font-size: 0.95rem;
  line-height: 1.6;
  white-space: pre-wrap;
}

.experience-entry {
  margin-bottom: 16px;
}

.experience-entry .entry-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 12px;
}

.experience-entry .entry-title {
  font-weight: 700;
  font-size: 1rem;
}

.experience-entry .entry-company {
  font-weight: 600;
  color: var(--color-primary);
}

.experience-entry .entry-date {
  font-size: 0.85rem;
  color: var(--color-muted);
  white-space: nowrap;
}

.experience-entry .entry-description {
  font-size: 0.9rem;
  line-height: 1.5;
  margin-top: 4px;
  white-space: pre-wrap;
}

.education-entry {
  margin-bottom: 8px;
}

.education-entry .edu-degree {
  font-weight: 700;
}

.education-entry .edu-school {
  color: var(--color-muted);
}

.skills-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

.skill-tag {
  background: #eff6ff;
  color: var(--color-primary-dark);
  border: 1px solid #bfdbfe;
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 0.85rem;
  font-weight: 500;
}

/* Cover letter */
.cover-letter {
  font-family: var(--font-sans);
  color: #1a1d21;
  font-size: 0.95rem;
  line-height: 1.6;
}

.cover-letter .sender-info {
  margin-bottom: 24px;
}

.cover-letter .sender-info p {
  margin: 0;
}

.cover-letter .date {
  margin-bottom: 24px;
}

.cover-letter .recipient-info {
  margin-bottom: 24px;
}

.cover-letter .recipient-info p {
  margin: 0;
}

.cover-letter .salutation {
  margin-bottom: 16px;
}

.cover-letter .body p {
  margin-bottom: 14px;
}

.cover-letter .closing {
  margin-top: 24px;
}

.cover-letter .closing p {
  margin: 0;
}

/* Print styles */
@media print {
  body {
    background: #fff;
  }

  .app-header,
  .form-panel,
  .preview-toolbar {
    display: none !important;
  }

  .app-layout {
    display: block;
    height: auto;
  }

  .preview-panel {
    background: #fff;
    overflow: visible;
  }

  .preview-scroll {
    padding: 0;
    overflow: visible;
    display: block;
  }

  #preview-content {
    box-shadow: none;
    border-radius: 0;
    padding: 0;
    max-width: 100%;
  }
}
```

### `app.js`

```javascript
(function () {
  'use strict';

  // ---------- Sample data ----------
  const sampleState = {
    personal: {
      name: 'Alex Johnson',
      title: 'Frontend Developer',
      email: 'alex.johnson@example.com',
      phone: '(555) 123-4567',
      location: 'San Francisco, CA',
      website: 'linkedin.com/in/alexjohnson'
    },
    summary: 'Results-driven frontend developer with 5+ years of experience building accessible, responsive web applications. Passionate about clean code, user experience, and mentoring junior developers.',
    experience: [
      {
        id: 1,
        title: 'Senior Frontend Developer',
        company: 'Tech Corp',
        location: 'San Francisco, CA',
        start: '2020',
        end: 'Present',
        description: 'Led a team of 5 developers to rebuild the company\'s flagship dashboard.\nIntroduced TypeScript and automated testing, reducing bugs by 40%.\nCollaborated with design to implement a new design system.'
      },
      {
        id: 2,
        title: 'Frontend Developer',
        company: 'Startup Inc',
        location: 'Remote',
        start: '2018',
        end: '2020',
        description: 'Built responsive, accessible web apps using React and Node.js.\nWorked closely with product managers to ship features on tight deadlines.\nMentored two junior developers and led code reviews.'
      }
    ],
    education: [
      {
        id: 1,
        degree: 'B.S. in Computer Science',
        school: 'University of California',
        year: '2018'
      }
    ],
    skills: ['JavaScript', 'React', 'CSS', 'Node.js', 'Accessibility', 'Git'],
    coverLetter: {
      recipient: 'Hiring Manager',
      company: 'Tech Corp',
      position: 'Senior Frontend Developer',
      notes: 'I am particularly excited about your focus on user experience and the opportunity to contribute to a collaborative team.'
    }
  };

  // ---------- State ----------
  let state = JSON.parse(JSON.stringify(sampleState));
  let activeTab = 'resume';
  let idCounter = 100;

  // ---------- DOM refs ----------
  const form = document.getElementById('resume-form');
  const experienceList = document.getElementById('experience-list');
  const educationList = document.getElementById('education-list');
  const previewContent = document.getElementById('preview-content');
  const tabResume = document.getElementById('tab-resume');
  const tabCover = document.getElementById('tab-cover');
  const printBtn = document.getElementById('print-btn');
  const resetBtn = document.getElementById('reset-btn');
  const addExperienceBtn = document.getElementById('add-experience-btn');
  const addEducationBtn = document.getElementById('add-education-btn');

  // ---------- Helpers ----------
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatDate() {
    const d = new Date();
    const options = { year: 'numeric', month: 'long', day: 'numeric' };
    return d.toLocaleDateString(undefined, options);
  }

  // ---------- Render form ----------
  function renderExperienceForm() {
    experienceList.innerHTML = '';
    state.experience.forEach((exp) => {
      const item = document.createElement('div');
      item.className = 'experience-item';
      item.dataset.id = exp.id;
      item.innerHTML = `
        <button type="button" class="remove-btn" aria-label="Remove experience">&times;</button>
        <div class="form-row">
          <label>Job Title</label>
          <input type="text" class="exp-title" value="${escapeHtml(exp.title)}">
        </div>
        <div class="form-row">
          <label>Company</label>
          <input type="text" class="exp-company" value="${escapeHtml(exp.company)}">
        </div>
        <div class="form-row">
          <label>Location</label>
          <input type="text" class="exp-location" value="${escapeHtml(exp.location)}">
        </div>
        <div class="form-row">
          <label>Start Date</label>
          <input type="text" class="exp-start" value="${escapeHtml(exp.start)}">
        </div>
        <div class="form-row">
          <label>End Date</label>
          <input type="text" class="exp-end" value="${escapeHtml(exp.end)}">
        </div>
        <div class="form-row">
          <label>Description (one bullet per line)</label>
          <textarea class="exp-description" rows="4">${escapeHtml(exp.description)}</textarea>
        </div>
      `;
      experienceList.appendChild(item);
    });
  }

  function renderEducationForm() {
    educationList.innerHTML = '';
    state.education.forEach((edu) => {
      const item = document.createElement('div');
      item.className = 'education-item';
      item.dataset.id = edu.id;
      item.innerHTML = `
        <button type="button" class="remove-btn" aria-label="Remove education">&times;</button>
        <div class="form-row">
          <label>Degree / Certificate</label>
          <input type="text" class="edu-degree" value="${escapeHtml(edu.degree)}">
        </div>
        <div class="form-row">
          <label>School / Institution</label>
          <input type="text" class="edu-school" value="${escapeHtml(edu.school)}">
        </div>
        <div class="form-row">
          <label>Year</label>
          <input type="text" class="edu-year" value="${escapeHtml(edu.year)}">
        </div>
      `;
      educationList.appendChild(item);
    });
  }

  function renderForm() {
    document.getElementById('personal-name').value = state.personal.name;
    document.getElementById('personal-title').value = state.personal.title;
    document.getElementById('personal-email').value = state.personal.email;
    document.getElementById('personal-phone').value = state.personal.phone;
    document.getElementById('personal-location').value = state.personal.location;
    document.getElementById('personal-website').value = state.personal.website;
    document.getElementById('summary').value = state.summary;
    document.getElementById('skills').value = state.skills.join(', ');
    document.getElementById('cover-recipient').value = state.coverLetter.recipient;
    document.getElementById('cover-company').value = state.coverLetter.company;
    document.getElementById('cover-position').value = state.coverLetter.position;
    document.getElementById('cover-notes').value = state.coverLetter.notes;
    renderExperienceForm();
    renderEducationForm();
  }

  // ---------- Read form into state ----------
  function readForm() {
    state.personal.name = document.getElementById('personal-name').value.trim();
    state.personal.title = document.getElementById('personal-title').value.trim();
    state.personal.email = document.getElementById('personal-email').value.trim();
    state.personal.phone = document.getElementById('personal-phone').value.trim();
    state.personal.location = document.getElementById('personal-location').value.trim();
    state.personal.website = document.getElementById('personal-website').value.trim();
    state.summary = document.getElementById('summary').value.trim();
    state.skills = document.getElementById('skills').value.split(',').map(s => s.trim()).filter(Boolean);
    state.coverLetter.recipient = document.getElementById('cover-recipient').value.trim();
    state.coverLetter.company = document.getElementById('cover-company').value.trim();
    state.coverLetter.position = document.getElementById('cover-position').value.trim();
    state.coverLetter.notes = document.getElementById('cover-notes').value.trim();

    // Experience
    state.experience = [];
    document.querySelectorAll('.experience-item').forEach((item) => {
      state.experience.push({
        id: Number(item.dataset.id),
        title: item.querySelector('.exp-title').value.trim(),
        company: item.querySelector('.exp-company').value.trim(),
        location: item.querySelector('.exp-location').value.trim(),
        start: item.querySelector('.exp-start').value.trim(),
        end: item.querySelector('.exp-end').value.trim(),
        description: item.querySelector('.exp-description').value.trim()
      });
    });

    // Education
    state.education = [];
    document.querySelectorAll('.education-item').forEach((item) => {
      state.education.push({
        id: Number(item.dataset.id),
        degree: item.querySelector('.edu-degree').value.trim(),
        school: item.querySelector('.edu-school').value.trim(),
        year: item.querySelector('.edu-year').value.trim()
      });
    });
  }

  // ---------- Render preview ----------
  function renderResume() {
    const p = state.personal;
    const contactParts = [p.email, p.phone, p.location, p.website].filter(Boolean);

    let html = '<div class="document">';
    html += `<h1>${escapeHtml(p.name)}</h1>`;
    if (p.title) html += `<div class="job-title">${escapeHtml(p.title)}</div>`;
    if (contactParts.length) {
      html += `<div class="contact-line">${contactParts.map(part => `<span>${escapeHtml(part)}</span>`).join('')}</div>`;
    }

    if (state.summary) {
      html += `<h2>Professional Summary</h2>`;
      html += `<div class="summary-text">${escapeHtml(state.summary)}</div>`;
    }

    if (state.experience.length) {
      html += `<h2>Experience</h2>`;
      state.experience.forEach((exp) => {
        html += `<div class="experience-entry">`;
        html += `<div class="entry-header">`;
        html += `<div><span class="entry-title">${escapeHtml(exp.title)}</span> — <span class="entry-company">${escapeHtml(exp.company)}</span>${exp.location ? `, ${escapeHtml(exp.location)}` : ''}</div>`;
        html += `<div class="entry-date">${escapeHtml(exp.start)} — ${escapeHtml(exp.end)}</div>`;
        html += `</div>`;
        if (exp.description) {
          html += `<div class="entry-description">${escapeHtml(exp.description)}</div>`;
        }
        html += `</div>`;
      });
    }

    if (state.education.length) {
      html += `<h2>Education</h2>`;
      state.education.forEach((edu) => {
        html += `<div class="education-entry">`;
        html += `<span class="edu-degree">${escapeHtml(edu.degree)}</span> — <span class="edu-school">${escapeHtml(edu.school)}</span>${edu.year ? `, ${escapeHtml(edu.year)}` : ''}`;
        html += `</div>`;
      });
    }

    if (state.skills.length) {
      html += `<h2>Skills</h2>`;
      html += `<div class="skills-list">`;
      state.skills.forEach(skill => {
        html += `<span class="skill-tag">${escapeHtml(skill)}</span>`;
      });
      html += `</div>`;
    }

    html += '</div>';
    return html;
  }

  function renderCoverLetter() {
    const p = state.personal;
    const cl = state.coverLetter;
    const currentJob = state.experience.find(exp => exp.end.toLowerCase() === 'present') || state.experience[0];
    const skillsText = state.skills.length ? state.skills.slice(0, 4).join(', ') : 'relevant skills';

    let html = '<div class="cover-letter">';

    // Sender info
    html += `<div class="sender-info">`;
    if (p.name) html += `<p><strong>${escapeHtml(p.name)}</strong></p>`;
    if (p.email) html += `<p>${escapeHtml(p.email)}</p>`;
    if (p.phone) html += `<p>${escapeHtml(p.phone)}</p>`;
    if (p.location) html += `<p>${escapeHtml(p.location)}</p>`;
    html += `</div>`;

    html += `<div class="date">${escapeHtml(formatDate())}</div>`;

    // Recipient info
    html += `<div class="recipient-info">`;
    if (cl.recipient) html += `<p>${escapeHtml(cl.recipient)}</p>`;
    if (cl.company) html += `<p>${escapeHtml(cl.company)}</p>`;
    if (cl.position) html += `<p>${escapeHtml(cl.position)}</p>`;
    html += `</div>`;

    html += `<div class="salutation">Dear ${escapeHtml(cl.recipient || 'Hiring Manager')},</div>`;

    html += `<div class="body">`;

    // Intro
    let intro = `I am writing to express my interest in the ${cl.position ? escapeHtml(cl.position) : 'open position'} at ${cl.company ? escapeHtml(cl.company) : 'your company'}. With my background as a ${p.title ? escapeHtml(p.title) : 'professional'} and experience in ${escapeHtml(skillsText)}, I am confident I would be a strong addition to your team.`;
    html += `<p>${intro}</p>`;

    // Experience paragraph
    if (currentJob) {
      const expText = `In my current role as ${escapeHtml(currentJob.title)} at ${escapeHtml(currentJob.company)}, I have honed my skills in ${escapeHtml(skillsText)}. ${currentJob.description ? escapeHtml(currentJob.description.split('\n')[0]) : ''}`;
      html += `<p>${expText}</p>`;
    } else if (state.summary) {
      html += `<p>${escapeHtml(state.summary)}</p>`;
    }

    // Additional notes
    if (cl.notes) {
      html += `<p>${escapeHtml(cl.notes)}</p>`;
    }

    // Closing
    html += `<p>Thank you for considering my application. I look forward to the opportunity to discuss how my skills and experience align with the needs of your team.</p>`;
    html += `</div>`;

    html += `<div class="closing">`;
    html += `<p>Sincerely,</p>`;
    html += `<p>${escapeHtml(p.name || '')}</p>`;
    html += `</div>`;

    html += '</div>';
    return html;
  }

  function renderPreview() {
    if (activeTab === 'resume') {
      previewContent.innerHTML = renderResume();
    } else {
      previewContent.innerHTML = renderCoverLetter();
    }
  }

  // ---------- Tab switching ----------
  function setActiveTab(tab) {
    activeTab = tab;
    if (tab === 'resume') {
      tabResume.classList.add('active');
      tabResume.setAttribute('aria-selected', 'true');
      tabCover.classList.remove('active');
      tabCover.setAttribute('aria-selected', 'false');
    } else {
      tabCover.classList.add('active');
      tabCover.setAttribute('aria-selected', 'true');
      tabResume.classList.remove('active');
      tabResume.setAttribute('aria-selected', 'false');
    }
    renderPreview();
  }

  // ---------- Add / remove dynamic items ----------
  function addExperience() {
    const newExp = {
      id: ++idCounter,
      title: '',
      company: '',
      location: '',
      start: '',
      end: '',
      description: ''
    };
    state.experience.push(newExp);
    renderExperienceForm();
    renderPreview();
    // Focus the first input of the new item
    const items = experienceList.querySelectorAll('.experience-item');
    const lastItem = items[items.length - 1];
    if (lastItem) {
      const input = lastItem.querySelector('.exp-title');
      if (input) input.focus();
    }
  }

  function addEducation() {
    const newEdu = {
      id: ++idCounter,
      degree: '',
      school: '',
      year: ''
    };
    state.education.push(newEdu);
    renderEducationForm();
    renderPreview();
    const items = educationList.querySelectorAll('.education-item');
    const lastItem = items[items.length - 1];
    if (lastItem) {
      const input = lastItem.querySelector('.edu-degree');
      if (input) input.focus();
    }
  }

  function removeExperience(id) {
    state.experience = state.experience.filter(exp => exp.id !== id);
    renderExperienceForm();
    renderPreview();
  }

  function removeEducation(id) {
    state.education = state.education.filter(edu => edu.id !== id);
    renderEducationForm();
    renderPreview();
  }

  // ---------- Event listeners ----------
  form.addEventListener('input', (e) => {
    if (e.target.closest('.experience-item') || e.target.closest('.education-item')) {
      // Update state from the specific item
      const item = e.target.closest('.experience-item') || e.target.closest('.education-item');
      if (item.classList.contains('experience-item')) {
        const id = Number(item.dataset.id);
        const exp = state.experience.find(x => x.id === id);
        if (exp) {
          exp.title = item.querySelector('.exp-title').value.trim();
          exp.company = item.querySelector('.exp-company').value.trim();
          exp.location = item.querySelector('.exp-location').value.trim();
          exp.start = item.querySelector('.exp-start').value.trim();
          exp.end = item.querySelector('.exp-end').value.trim();
          exp.description = item.querySelector('.exp-description').value.trim();
        }
      } else if (item.classList.contains('education-item')) {
        const id = Number(item.dataset.id);
        const edu = state.education.find(x => x.id === id);
        if (edu) {
          edu.degree = item.querySelector('.edu-degree').value.trim();
          edu.school = item.querySelector('.edu-school').value.trim();
          edu.year = item.querySelector('.edu-year').value.trim();
        }
      }
    } else {
      readForm();
    }
    renderPreview();
  });

  experienceList.addEventListener('click', (e) => {
    const removeBtn = e.target.closest('.remove-btn');
    if (!removeBtn) return;
    const item = removeBtn.closest('.experience-item');
    if (item) {
      removeExperience(Number(item.dataset.id));
    }
  });

  educationList.addEventListener('click', (e) => {
    const removeBtn = e.target.closest('.remove-btn');
    if (!removeBtn) return;
    const item = removeBtn.closest('.education-item');
    if (item) {
      removeEducation(Number(item.dataset.id));
    }
  });

  addExperienceBtn.addEventListener('click', addExperience);
  addEducationBtn.addEventListener('click', addEducation);

  tabResume.addEventListener('click', () => setActiveTab('resume'));
  tabCover.addEventListener('click', () => setActiveTab('cover'));

  printBtn.addEventListener('click', () => {
    window.print();
  });

  resetBtn.addEventListener('click', () => {
    state = JSON.parse(JSON.stringify(sampleState));
    idCounter = 100;
    renderForm();
    renderPreview();
  });

  // ---------- Init ----------
  renderForm();
  renderPreview();
})();
```

### `Dockerfile`

```text
FROM nginx:alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html styles.css app.js /usr/share/nginx/html/
EXPOSE 3000
```

### `nginx.conf`

```text
events {
    worker_connections 1024;
}

http {
    server {
        listen 3000;
        server_name localhost;
        root /usr/share/nginx/html;
        index index.html;
        location / {
            try_files $uri $uri/ =404;
        }
    }
}
```

### `.github/workflows/ci.yml`

```yaml
name: CI

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
      - name: Build Docker image
        run: |
          docker build -t app .
```
