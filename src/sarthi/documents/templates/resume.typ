// Sarthi resume template. Single column, real text, no tables or images: what applicant tracking systems read
// best. All content comes from sys.inputs as JSON and is shown as plain text (never evaluated as markup).
#let data = json(bytes(sys.inputs.at("data")))
#let accent = rgb("#1f3a5f")
#let muted = luma(85)
#set document(title: data.name + " - Resume", author: data.name)
#set page(paper: data.paper, margin: (x: 1.6cm, top: 1.3cm, bottom: 1.2cm))
#set text(font: ("Liberation Sans", "Arial", "Helvetica", "DejaVu Sans", "Libertinus Serif"), size: 10pt, lang: "en", ligatures: false)
#set par(justify: false, leading: 0.52em, spacing: 0.55em)
#set list(indent: 0.15em, body-indent: 0.55em, spacing: 0.42em, marker: text(fill: accent, size: 8pt, baseline: -0.05em)[●])

#let heading-line(title) = {
  v(0.8em)
  text(size: 10.5pt, weight: "bold", fill: accent, tracking: 0.06em, upper(title))
  v(-0.3em)
  line(length: 100%, stroke: 0.6pt + accent)
  v(0.05em)
}
#let when(start, end) = if start != "" or end != "" {
  text(fill: muted, size: 9.5pt)[#start#if start != "" and end != "" [ – ]#end]
}
#let row(what, dates) = grid(columns: (1fr, auto), column-gutter: 1em, what, align(right, dates))
#let shown(value) = value.replace(regex("^https?://(www\.)?"), "").trim("/")
#let contact-item(value) = if value.contains("@") and not value.contains("/") {
  link("mailto:" + value, value)
} else if value.starts-with("http") {
  link(value, shown(value))
} else { value }

// ---- header ---------------------------------------------------------------------------------------------
#align(center)[
  #text(size: 21pt, weight: "bold", fill: accent, data.name)
  #if data.headline != "" { v(-0.2em); text(size: 11pt, fill: muted, data.headline) }
  #v(-0.05em)
  #text(size: 9pt, data.contact.map(contact-item).join(text(fill: muted)[#h(0.45em)|#h(0.45em)]))
]

#if data.summary != "" {
  heading-line("Summary")
  data.summary
}

#if data.experience.len() > 0 {
  heading-line("Experience")
  for (i, job) in data.experience.enumerate() {
    if i > 0 { v(0.35em) }
    row(text(weight: "bold", job.title), when(job.start, job.end))
    v(-0.2em)
    text(fill: muted, style: "italic")[#job.company#if job.location != "" [, #job.location]]
    if job.bullets.len() > 0 { v(-0.15em); list(..job.bullets.map(b => [#b])) }
  }
}

#if data.projects.len() > 0 {
  heading-line("Projects")
  for (i, project) in data.projects.enumerate() {
    if i > 0 { v(0.35em) }
    let name = text(weight: "bold", project.name)
    row(name, if project.link != "" { text(size: 9pt, contact-item(project.link)) })
    let points = if project.description != "" { (project.description,) } else { () }
    points += project.bullets
    if points.len() > 0 { v(-0.15em); list(..points.map(p => [#p])) }
  }
}

#if data.skills.len() > 0 {
  heading-line("Skills")
  let labelled = data.skills.filter(g => g.label != "")
  if labelled.len() == data.skills.len() {
    grid(
      columns: (auto, 1fr), column-gutter: 0.9em, row-gutter: 0.5em,
      ..data.skills.map(g => (text(weight: "bold", g.label), g.items.join(", "))).flatten(),
    )
  } else {
    for group in data.skills {
      [#if group.label != "" [*#group.label:* ]#group.items.join(", ")]
      parbreak()
    }
  }
}

#if data.education.len() > 0 {
  heading-line("Education")
  for (i, school) in data.education.enumerate() {
    if i > 0 { v(0.3em) }
    row(text(weight: "bold", if school.degree != "" { school.degree } else { school.school }), when(school.start, school.end))
    if school.degree != "" { v(-0.2em); text(fill: muted, style: "italic", school.school) }
    if school.details.len() > 0 { v(-0.15em); list(..school.details.map(d => [#d])) }
  }
}

#for extra in data.extras {
  if extra.items.len() > 0 {
    heading-line(extra.heading)
    list(..extra.items.map(i => [#i]))
  }
}
