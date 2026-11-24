/** Nonserialized original-turn witness installed only after Host admission. */
export const browserSnapshotAdmission=Symbol('browser-snapshot-admission')
export type BrowserSnapshotWitness={current:()=>boolean}
