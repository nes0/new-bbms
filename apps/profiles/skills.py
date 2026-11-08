"""
The skills a member can list on their profile (spec 06 §6.1), with the keys legacy used (so its profiles import).

Icons are from The Noun Project, licensed CC BY, so each is credited on the credits page (`CREDITS`). Arts and crafts
has no icon: legacy's had no licence file.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    key: str
    name: str
    icon: str = ""  # a file in static/img/skills/
    credit: str = ""


SKILLS = [
    Skill("3dprinting", "3D printing", "3dprinting.svg", "3D Printing by Andrew Cameron"),
    Skill("arduino", "Arduino", "arduino.svg", "Arduino by uizin"),
    Skill("art-craft", "Arts and crafts"),
    Skill("audio", "Audio", "audio.svg", "Speaker by deadtype"),
    Skill("biology", "Biology", "biology.svg", "Microscope by Stuart McCoy"),
    Skill("cad", "CAD", "cad.svg", "3D Model by José Sanmartín González"),
    Skill("coding", "Coding", "coding.svg", "CSS by Q. Li"),
    Skill("electronics", "Electronics", "electronics.svg", "LED by jon trillana"),
    Skill("graphic-design", "Graphic design", "graphic-design.svg", "Graphic Design by Cornelius Danger"),
    Skill("laser-cutter", "Laser cutter", "laser-cutter.svg", "Laser Cutter by Rhys Dezsery"),
    Skill("microcontroller", "Microcontrollers", "microcontroller.svg", "Microchip by Nicolò Bertoncin"),
    Skill("midi", "MIDI", "midi.svg", "MIDI by Kurt Lorenz"),
    Skill("networking", "Networking", "networking.svg", "LAN Cable by Nikhil Dev"),
    Skill("pc-building", "PC building", "pc-building.svg", "Hard Drive by Rigo Peter"),
    Skill("pcb-design", "PCB design and manufacture", "pcb-design.svg", "Circuit Board by Mike Wirth"),
    Skill("printing", "Printing", "printing.svg", "Drops by j.ledkov"),
    Skill("retro-computing", "Retro computing", "retro-computing.svg", "Computer by Yorlmar Campos"),
    Skill("sewing", "Sewing", "sewing.svg", "Needle and Thread by José Manuel de Laá"),
    Skill("social-media", "Social media", "social-media.svg", "Social Media by Milky - Digital innovation"),
    Skill("welding", "Welding", "welding.svg", "Welder by Ross Naumov"),
    Skill("wood-work", "Woodworking", "wood-work.svg", "Saw by Alberto Valz Gris"),
]
BY_KEY = {skill.key: skill for skill in SKILLS}
CHOICES = [(skill.key, skill.name) for skill in SKILLS]


def skills_for(keys) -> list[Skill]:
    """The member's skills, in the list's order, ignoring keys that aren't on it any more."""
    wanted = set(keys or [])
    return [skill for skill in SKILLS if skill.key in wanted]
