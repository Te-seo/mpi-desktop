/** Put the precise threshold immediately above its own dashed line. */
export function memoryReferenceLabel(label: string) {
  return ({ viewBox }: { viewBox?: { x?: number; y?: number; width?: number } }) => {
    if (!viewBox || typeof viewBox.x !== 'number' || typeof viewBox.y !== 'number' ||
        typeof viewBox.width !== 'number') return <g />
    return <text x={viewBox.x + viewBox.width - 3} y={viewBox.y - 6} textAnchor="end"
      className="fill-amber-800 text-[11px] font-medium dark:fill-amber-300" pointerEvents="none">
      {label}
    </text>
  }
}
