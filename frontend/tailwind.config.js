/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        match:    { bg: '#dcfce7', text: '#166534', border: '#86efac' },
        nomatch:  { bg: '#fee2e2', text: '#991b1b', border: '#fca5a5' },
        nocsv:    { bg: '#fef9c3', text: '#854d0e', border: '#fde047' },
        noinv:    { bg: '#f3f4f6', text: '#4b5563', border: '#d1d5db' },
      },
    },
  },
  plugins: [],
}
