import { cn } from '../lib/cn';

/** 图标路径（16px、stroke 1.7）。 */
const ICON_PATHS = {
  library: ['M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h7A1.5 1.5 0 0 1 19 10v6.5A1.5 1.5 0 0 1 17.5 18h-13A1.5 1.5 0 0 1 3 16.5z'],
  glossary: [
    'M5 4.5h9.5A2.5 2.5 0 0 1 17 7v12H7.5A2.5 2.5 0 0 1 5 16.5z',
    'M17 7h2v12H7.5',
    'M8.5 8.5h5M8.5 11.5h5',
  ],
  settings: [
    'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
    'M12 3.5v2M12 18.5v2M4.9 7.5l1.7 1M17.4 15.5l1.7 1M4.9 16.5l1.7-1M17.4 8.5l1.7-1',
  ],
  progress: ['M3 12h3.5l2.5 6 4-13 2.5 7H21'],
  recognize: [
    'M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16M4 12h16',
  ],
  translate: [
    'M4 6h9M8.5 6v2c0 4-2 6-4.5 7M6 13c1.5 2 3.5 3 5.5 3.5M13 20l3.5-9 3.5 9M14.4 17h5.2',
  ],
  check: ['M12 3.5l7 3v5c0 4-3 7.5-7 9-4-1.5-7-5-7-9v-5zM9.2 12.2l2 2 3.6-4'],
  archive: [
    'M4 7.5h16v11a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5zM3.5 4.5h17v3h-17zM10 11.5h4',
  ],
  upload: ['M12 19V6M7 11l5-5 5 5'],
  drop: ['M12 16V5M8 9l4-4 4 4', 'M4.5 15v3.5h15V15'],
} as const;

export type IconName = keyof typeof ICON_PATHS;

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className={cn(
        'h-4 w-4 flex-none fill-none stroke-current [stroke-linecap:round] [stroke-linejoin:round] [stroke-width:1.7]',
        className,
      )}
    >
      {ICON_PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
