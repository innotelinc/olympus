import { describe, expect, it } from "vitest";
import { buildPreviewDocument, languageFor, parseFiles } from "@/lib/files";

const ONE_FILE = `<file path="index.html">
<!doctype html><html><head><title>Counter</title></head>
<body><p id="c">0</p><script>let n = 0;</script></body></html>
</file>`;

describe("parseFiles", () => {
  it("extracts a file block and its path", () => {
    const files = parseFiles(ONE_FILE);
    expect(files).toHaveLength(1);
    expect(files[0].path).toBe("index.html");
    expect(files[0].contents).toContain("let n = 0;");
  });

  it("does not leave a leading newline after the tag", () => {
    expect(parseFiles(ONE_FILE)[0].contents.startsWith("\n")).toBe(false);
  });

  it("ignores a block whose closing tag has not streamed in yet", () => {
    expect(parseFiles('<file path="index.html">\n<!doctype html><html>')).toHaveLength(0);
  });

  it("reports the blocks that are complete during a stream", () => {
    const partial = `${ONE_FILE}\n<file path="app.js">\nconsole.log("partial");`;

    expect(parseFiles(partial)).toHaveLength(1);
    expect(parseFiles(`${partial}\n</file>`)).toHaveLength(2);
  });

  it("extracts multiple files in order", () => {
    const files = parseFiles(`${ONE_FILE}\n<file path="app.js">\nconsole.log("hi");\n</file>`);
    expect(files.map((file) => file.path)).toEqual(["index.html", "app.js"]);
  });

  it("keeps the first occurrence of a repeated path", () => {
    expect(parseFiles(`${ONE_FILE}\n${ONE_FILE}`)).toHaveLength(1);
  });

  it("returns nothing for empty input or prose", () => {
    expect(parseFiles("")).toHaveLength(0);
    expect(parseFiles("Sure! Here is your app.")).toHaveLength(0);
  });

  it("tolerates extra whitespace inside the tag", () => {
    const files = parseFiles('<file  path="styles.css"  >\nbody { color: red; }\n</file>');
    expect(files[0].path).toBe("styles.css");
  });
});

describe("buildPreviewDocument", () => {
  const multi = `prose the model should not have emitted
<file path="index.html">
<!doctype html>
<html>
<head>
  <title>Tip splitter</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div id="app"></div>
  <script src="app.js"></script>
</body>
</html>
</file>
<file path="styles.css">
body { background: rebeccapurple; }
</file>
<file path="app.js">
document.getElementById("app").textContent = "ready";
</file>`;

  it("inlines linked stylesheets and scripts", () => {
    const document = buildPreviewDocument(parseFiles(multi));
    expect(document).toContain("rebeccapurple");
    expect(document).toContain('textContent = "ready"');
    expect(document).not.toContain('href="styles.css"');
    expect(document).not.toContain('src="app.js"');
  });

  it("does not duplicate inlined assets as orphans", () => {
    const document = buildPreviewDocument(parseFiles(multi));
    expect(document.split("rebeccapurple")).toHaveLength(2);
    expect(document.split("textContent")).toHaveLength(2);
  });

  it("appends assets the entry point never referenced", () => {
    const document = buildPreviewDocument(
      parseFiles(
        '<file path="index.html">\n<html><head></head><body>hi</body></html>\n</file>\n<file path="styles.css">\nh1 { color: teal; }\n</file>',
      ),
    );
    expect(document).toContain("teal");
    expect(document.indexOf("teal")).toBeLessThan(document.indexOf("</head>"));
  });

  it("appends orphaned scripts before the closing body tag", () => {
    const document = buildPreviewDocument(
      parseFiles(
        '<file path="index.html">\n<html><head></head><body>hi</body></html>\n</file>\n<file path="app.js">\nconsole.log("late");\n</file>',
      ),
    );
    expect(document.indexOf('console.log("late")')).toBeLessThan(document.indexOf("</body>"));
  });

  it("prefers index.html as the entry point", () => {
    const document = buildPreviewDocument(
      parseFiles(
        '<file path="about.html">\n<html><body>ABOUT</body></html>\n</file>\n<file path="index.html">\n<html><body>HOME</body></html>\n</file>',
      ),
    );
    expect(document).toContain("HOME");
    expect(document).not.toContain("ABOUT");
  });

  it("falls back to a listing document when there is no HTML", () => {
    const document = buildPreviewDocument(parseFiles('<file path="data.json">\n{"a":1}\n</file>'));
    expect(document).toContain("No HTML entry point");
    expect(document).toContain("data.json");
  });

  it("returns the placeholder for an empty file set", () => {
    expect(buildPreviewDocument([])).toContain("Nothing rendered yet");
  });

  it("keeps a traversal path inert", () => {
    const document = buildPreviewDocument(parseFiles('<file path="../../etc/passwd">\nroot:x:0:0\n</file>'));
    expect(document).not.toContain("root:x:0:0");
  });

  it("escapes file names in the fallback listing", () => {
    const document = buildPreviewDocument(
      parseFiles('<file path="<img src=x onerror=alert(1)>.json">\n{}\n</file>'),
    );
    expect(document).not.toContain("<img src=x");
    expect(document).toContain("&lt;img");
  });
});

describe("languageFor", () => {
  it("labels files by extension", () => {
    expect(languageFor("index.html")).toBe("html");
    expect(languageFor("styles.css")).toBe("css");
    expect(languageFor("app.js")).toBe("javascript");
    expect(languageFor("nested/assets/logo.svg")).toBe("svg");
    expect(languageFor("notes.txt")).toBe("text");
  });
});
