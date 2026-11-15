// Xenon Holo - periodic table data for the PERIODIC LAB mode (table.js).
// ELEMENTS: all 118 elements {z, sym, name, mass, cat, group, period, f}
//   group/period are the standard positions; lanthanides (La-Lu) and actinides
//   (Ac-Lr) carry group 3 and f = 0..14, their place in the two detached rows.
// MOLECULES: real molecules with 3D geometry (Angstrom) and bonds [i, j, order].
// matchMolecule(counts) -> the molecule whose element counts equal counts, or null.

const RAW = `1 H Hydrogen 1.008|2 He Helium 4.0026|3 Li Lithium 6.94|4 Be Beryllium 9.0122|5 B Boron 10.81|6 C Carbon 12.011|7 N Nitrogen 14.007|8 O Oxygen 15.999|9 F Fluorine 18.998|10 Ne Neon 20.180|
11 Na Sodium 22.990|12 Mg Magnesium 24.305|13 Al Aluminium 26.982|14 Si Silicon 28.085|15 P Phosphorus 30.974|16 S Sulfur 32.06|17 Cl Chlorine 35.45|18 Ar Argon 39.95|19 K Potassium 39.098|20 Ca Calcium 40.078|
21 Sc Scandium 44.956|22 Ti Titanium 47.867|23 V Vanadium 50.942|24 Cr Chromium 51.996|25 Mn Manganese 54.938|26 Fe Iron 55.845|27 Co Cobalt 58.933|28 Ni Nickel 58.693|29 Cu Copper 63.546|30 Zn Zinc 65.38|
31 Ga Gallium 69.723|32 Ge Germanium 72.630|33 As Arsenic 74.922|34 Se Selenium 78.971|35 Br Bromine 79.904|36 Kr Krypton 83.798|37 Rb Rubidium 85.468|38 Sr Strontium 87.62|39 Y Yttrium 88.906|40 Zr Zirconium 91.224|
41 Nb Niobium 92.906|42 Mo Molybdenum 95.95|43 Tc Technetium 98|44 Ru Ruthenium 101.07|45 Rh Rhodium 102.91|46 Pd Palladium 106.42|47 Ag Silver 107.87|48 Cd Cadmium 112.41|49 In Indium 114.82|50 Sn Tin 118.71|
51 Sb Antimony 121.76|52 Te Tellurium 127.60|53 I Iodine 126.90|54 Xe Xenon 131.29|55 Cs Caesium 132.91|56 Ba Barium 137.33|57 La Lanthanum 138.91|58 Ce Cerium 140.12|59 Pr Praseodymium 140.91|60 Nd Neodymium 144.24|
61 Pm Promethium 145|62 Sm Samarium 150.36|63 Eu Europium 151.96|64 Gd Gadolinium 157.25|65 Tb Terbium 158.93|66 Dy Dysprosium 162.50|67 Ho Holmium 164.93|68 Er Erbium 167.26|69 Tm Thulium 168.93|70 Yb Ytterbium 173.05|
71 Lu Lutetium 174.97|72 Hf Hafnium 178.49|73 Ta Tantalum 180.95|74 W Tungsten 183.84|75 Re Rhenium 186.21|76 Os Osmium 190.23|77 Ir Iridium 192.22|78 Pt Platinum 195.08|79 Au Gold 196.97|80 Hg Mercury 200.59|
81 Tl Thallium 204.38|82 Pb Lead 207.2|83 Bi Bismuth 208.98|84 Po Polonium 209|85 At Astatine 210|86 Rn Radon 222|87 Fr Francium 223|88 Ra Radium 226|89 Ac Actinium 227|90 Th Thorium 232.04|
91 Pa Protactinium 231.04|92 U Uranium 238.03|93 Np Neptunium 237|94 Pu Plutonium 244|95 Am Americium 243|96 Cm Curium 247|97 Bk Berkelium 247|98 Cf Californium 251|99 Es Einsteinium 252|100 Fm Fermium 257|
101 Md Mendelevium 258|102 No Nobelium 259|103 Lr Lawrencium 266|104 Rf Rutherfordium 267|105 Db Dubnium 268|106 Sg Seaborgium 269|107 Bh Bohrium 270|108 Hs Hassium 277|109 Mt Meitnerium 278|110 Ds Darmstadtium 281|
111 Rg Roentgenium 282|112 Cn Copernicium 285|113 Nh Nihonium 286|114 Fl Flerovium 289|115 Mc Moscovium 290|116 Lv Livermorium 293|117 Ts Tennessine 294|118 Og Oganesson 294`;

const CATS = {
  alkali: 'Li Na K Rb Cs Fr', alkaline: 'Be Mg Ca Sr Ba Ra', nonmetal: 'H C N O P S Se', halogen: 'F Cl Br I At Ts',
  noble: 'He Ne Ar Kr Xe Rn Og', metalloid: 'B Si Ge As Sb Te', 'post-transition': 'Al Ga In Sn Tl Pb Bi Po Nh Fl Mc Lv',
};
const CAT_OF = {};
for (const [c, list] of Object.entries(CATS)) for (const s of list.split(' ')) CAT_OF[s] = c;

// Standard position from the atomic number.
export function position(z) {
  const starts = [1, 3, 11, 19, 37, 55, 87], ends = [2, 10, 18, 36, 54, 86, 118];
  const p = ends.findIndex(e => z <= e) + 1, k = z - starts[p - 1];
  if (p === 1) return {period: 1, group: z === 1 ? 1 : 18, f: null};
  if (p <= 3) return {period: p, group: k < 2 ? k + 1 : k + 11, f: null};
  if (p <= 5) return {period: p, group: k + 1, f: null};
  if (k < 2) return {period: p, group: k + 1, f: null};
  if (k <= 16) return {period: p, group: 3, f: k - 2};          // La-Lu / Ac-Lr
  return {period: p, group: k - 13, f: null};
}

export const ELEMENTS = RAW.split('|').map(s => s.trim()).filter(Boolean).map(s => {
  const [z, sym, name, mass] = s.split(' ');
  const pos = position(+z);
  const cat = pos.f !== null ? (pos.period === 6 ? 'lanthanide' : 'actinide') : CAT_OF[sym] || 'transition';
  return {z: +z, sym, name, mass: +mass, cat, group: pos.group, period: pos.period, f: pos.f};
});
export const BY_SYM = Object.fromEntries(ELEMENTS.map(e => [e.sym, e]));

export const CATEGORY_NAMES = {
  alkali: 'ALKALI METAL', alkaline: 'ALKALINE EARTH', transition: 'TRANSITION METAL', 'post-transition': 'POST-TRANSITION METAL',
  metalloid: 'METALLOID', nonmetal: 'NONMETAL', halogen: 'HALOGEN', noble: 'NOBLE GAS', lanthanide: 'LANTHANIDE', actinide: 'ACTINIDE',
};

// ---- molecules ---------------------------------------------------------------------
const tetra = (cx, cy, cz, d) => [[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]].map(v => [cx + v[0] * d / Math.sqrt(3), cy + v[1] * d / Math.sqrt(3), cz + v[2] * d / Math.sqrt(3)]);
function ethane() {
  const atoms = [['C', .765, 0, 0], ['C', -.765, 0, 0]], bonds = [[0, 1, 1]];
  const hx = .765 + 1.09 * Math.cos(69 * Math.PI / 180), r = 1.09 * Math.sin(69 * Math.PI / 180);
  for (let i = 0; i < 3; i++) { const a = i * 2 * Math.PI / 3; atoms.push(['H', hx, r * Math.cos(a), r * Math.sin(a)]); bonds.push([0, atoms.length - 1, 1]); }
  for (let i = 0; i < 3; i++) { const a = i * 2 * Math.PI / 3 + Math.PI / 3; atoms.push(['H', -hx, r * Math.cos(a), r * Math.sin(a)]); bonds.push([1, atoms.length - 1, 1]); }
  return {atoms, bonds};
}
function benzene() {
  const atoms = [], bonds = [];
  for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; atoms.push(['C', 1.39 * Math.cos(a), 1.39 * Math.sin(a), 0]); }
  for (let i = 0; i < 6; i++) { const a = i * Math.PI / 3; atoms.push(['H', 2.48 * Math.cos(a), 2.48 * Math.sin(a), 0]); bonds.push([i, (i + 1) % 6, i % 2 ? 1 : 2], [i, i + 6, 1]); }
  return {atoms, bonds};
}
function methanol() {
  const atoms = [['C', -.71, 0, 0], ['O', .71, 0, 0], ['H', 1.05, .89, 0]], bonds = [[0, 1, 1], [1, 2, 1]];
  const hx = -.71 - 1.09 * Math.cos(70.5 * Math.PI / 180), r = 1.09 * Math.sin(70.5 * Math.PI / 180);
  for (let i = 0; i < 3; i++) { const a = i * 2 * Math.PI / 3 + Math.PI / 3 + Math.PI / 2; atoms.push(['H', hx, r * Math.cos(a), r * Math.sin(a)]); bonds.push([0, atoms.length - 1, 1]); }
  return {atoms, bonds};
}
const ch4 = tetra(0, 0, 0, 1.09);

export const MOLECULES = [
  {formula: 'H2O', name: 'Water', fact: 'A bent 104.5 degree molecule; its hydrogen bonds make ice float.',
    atoms: [['O', 0, -.12, 0], ['H', .757, .466, 0], ['H', -.757, .466, 0]], bonds: [[0, 1, 1], [0, 2, 1]]},
  {formula: 'CO2', name: 'Carbon Dioxide', fact: 'Perfectly linear, so its two polar bonds cancel out.',
    atoms: [['C', 0, 0, 0], ['O', 1.16, 0, 0], ['O', -1.16, 0, 0]], bonds: [[0, 1, 2], [0, 2, 2]]},
  {formula: 'CH4', name: 'Methane', fact: 'The simplest hydrocarbon: four hydrogens at the corners of a tetrahedron.',
    atoms: [['C', 0, 0, 0], ...ch4.map(p => ['H', ...p])], bonds: [[0, 1, 1], [0, 2, 1], [0, 3, 1], [0, 4, 1]]},
  {formula: 'NaCl', name: 'Sodium Chloride', fact: 'Table salt; in a crystal each ion sits among six of the other kind.',
    atoms: [['Na', -1.18, 0, 0], ['Cl', 1.18, 0, 0]], bonds: [[0, 1, 1]]},
  {formula: 'NH3', name: 'Ammonia', fact: 'A trigonal pyramid that flips inside out billions of times a second.',
    atoms: [['N', 0, .38, 0], ['H', .94, 0, 0], ['H', -.47, 0, .814], ['H', -.47, 0, -.814]], bonds: [[0, 1, 1], [0, 2, 1], [0, 3, 1]]},
  {formula: 'O2', name: 'Oxygen', fact: 'Its two unpaired electrons make liquid oxygen stick to a magnet.',
    atoms: [['O', -.604, 0, 0], ['O', .604, 0, 0]], bonds: [[0, 1, 2]]},
  {formula: 'N2', name: 'Nitrogen', fact: 'The triple bond is one of the strongest in chemistry; 78% of air.',
    atoms: [['N', -.55, 0, 0], ['N', .55, 0, 0]], bonds: [[0, 1, 3]]},
  {formula: 'H2', name: 'Hydrogen', fact: 'The lightest molecule there is, and the fuel of the stars.',
    atoms: [['H', -.37, 0, 0], ['H', .37, 0, 0]], bonds: [[0, 1, 1]]},
  {formula: 'CO', name: 'Carbon Monoxide', fact: 'Binds haemoglobin about 200 times more tightly than oxygen does.',
    atoms: [['C', -.564, 0, 0], ['O', .564, 0, 0]], bonds: [[0, 1, 3]]},
  {formula: 'HCl', name: 'Hydrogen Chloride', fact: 'Dissolved in water it becomes hydrochloric acid, as in your stomach.',
    atoms: [['H', -.64, 0, 0], ['Cl', .64, 0, 0]], bonds: [[0, 1, 1]]},
  {formula: 'H2O2', name: 'Hydrogen Peroxide', fact: 'Twisted like an open book, with a weak O-O bond that likes to break.',
    atoms: [['O', .7375, 0, 0], ['O', -.7375, 0, 0], ['H', .96, .93, 0], ['H', -.96, .93 * Math.cos(111 * Math.PI / 180), .93 * Math.sin(111 * Math.PI / 180)]],
    bonds: [[0, 1, 1], [0, 2, 1], [1, 3, 1]]},
  {formula: 'C2H6', name: 'Ethane', fact: 'Its two methyl groups spin about the C-C bond, preferring a staggered pose.', ...ethane()},
  {formula: 'C2H4', name: 'Ethylene', fact: 'A flat plant hormone that ripens fruit; the double bond locks it planar.',
    atoms: [['C', .667, 0, 0], ['C', -.667, 0, 0], ['H', 1.23, .92, 0], ['H', 1.23, -.92, 0], ['H', -1.23, .92, 0], ['H', -1.23, -.92, 0]],
    bonds: [[0, 1, 2], [0, 2, 1], [0, 3, 1], [1, 4, 1], [1, 5, 1]]},
  {formula: 'C2H2', name: 'Acetylene', fact: 'Burns at over 3000 C with oxygen, hot enough to cut steel.',
    atoms: [['C', .6, 0, 0], ['C', -.6, 0, 0], ['H', 1.66, 0, 0], ['H', -1.66, 0, 0]], bonds: [[0, 1, 3], [0, 2, 1], [1, 3, 1]]},
  {formula: 'C6H6', name: 'Benzene', fact: 'Its six pi electrons are shared round the whole ring, not fixed in place.', ...benzene()},
  {formula: 'HCN', name: 'Hydrogen Cyanide', fact: 'Linear and lethal; also found in comet tails and bitter almonds.',
    atoms: [['H', -1.065, 0, 0], ['C', 0, 0, 0], ['N', 1.156, 0, 0]], bonds: [[0, 1, 1], [1, 2, 3]]},
  {formula: 'SO2', name: 'Sulfur Dioxide', fact: 'The sharp smell of a struck match; volcanoes breathe it out.',
    atoms: [['S', 0, .36, 0], ['O', 1.232, -.366, 0], ['O', -1.232, -.366, 0]], bonds: [[0, 1, 2], [0, 2, 2]]},
  {formula: 'O3', name: 'Ozone', fact: 'High in the stratosphere this bent molecule soaks up ultraviolet light.',
    atoms: [['O', 0, .33, 0], ['O', 1.088, -.33, 0], ['O', -1.088, -.33, 0]], bonds: [[0, 1, 2], [0, 2, 1]]},
  {formula: 'CH3OH', name: 'Methanol', fact: 'The simplest alcohol, once distilled from wood.', ...methanol()},
  {formula: 'NO', name: 'Nitric Oxide', fact: 'A tiny radical your blood vessels use as a signal to relax.',
    atoms: [['N', -.575, 0, 0], ['O', .575, 0, 0]], bonds: [[0, 1, 2]]},
];

// Count atoms from a list [[sym, ...], ...] or from a formula string like 'C2H6'.
export function countAtoms(atoms) { const c = {}; for (const a of atoms) c[a[0]] = (c[a[0]] || 0) + 1; return c; }
export function parseFormula(f) {
  const c = {}; const re = /([A-Z][a-z]?)(\d*)/g; let m;
  while ((m = re.exec(f))) if (m[1]) c[m[1]] = (c[m[1]] || 0) + (m[2] ? +m[2] : 1);
  return c;
}
for (const m of MOLECULES) m.counts = countAtoms(m.atoms);

export function sameCounts(a, b) {
  const clean = o => Object.entries(o || {}).filter(([, n]) => n > 0);
  const A = clean(a), B = clean(b);
  return A.length === B.length && A.every(([k, n]) => (b[k] || 0) === n);
}
export function matchMolecule(counts) {
  if (!counts || typeof counts !== 'object') return null;
  return MOLECULES.find(m => sameCounts(m.counts, counts)) || null;
}
