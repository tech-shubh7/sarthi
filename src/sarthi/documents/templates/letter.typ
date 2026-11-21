// Sarthi cover letter template, matching the resume. Content comes from sys.inputs as JSON, shown as plain text.
#let data = json(bytes(sys.inputs.at("data")))
#let accent = rgb("#1f3a5f")
#let muted = luma(85)
#set document(title: data.name + " - Cover letter", author: data.name)
#set page(paper: data.paper, margin: (x: 2.3cm, top: 2cm, bottom: 2cm))
#set text(font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"), size: 10.5pt, lang: "en", ligatures: false)
#set par(justify: false, leading: 0.68em, spacing: 1.15em)

#let shown(value) = value.replace(regex("^https?://(www\.)?"), "").trim("/")
#let contact-item(value) = if value.contains("@") and not value.contains("/") {
  link("mailto:" + value, value)
} else if value.starts-with("http") {
  link(value, shown(value))
} else { value }

#text(size: 18pt, weight: "bold", fill: accent, data.name)
#v(-0.6em)
#text(size: 9pt, data.contact.map(contact-item).join(text(fill: muted)[#h(0.45em)|#h(0.45em)]))
#v(-0.5em)
#line(length: 100%, stroke: 0.6pt + accent)
#v(0.6em)
#text(fill: muted, data.date)
#v(0.4em)
#data.greeting

#for paragraph in data.paragraphs [#paragraph

]
#v(0.2em)
#for line in data.sign_off.split("\n") [#line \ ]
