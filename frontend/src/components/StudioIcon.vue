<script setup>
defineProps({name: {type: String, default: 'sparkles'}, size: {type: [Number, String], default: 20}});
const paths = {
  home:['M3 10 12 3l9 7v10H3z','M9 20v-7h6v7'], sparkles:['m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7z'],
  film:['M3 4h18v16H3z','M7 4v16','M17 4v16','M3 9h4','M3 15h4','M17 9h4','M17 15h4'],
  music:['M9 18V5l12-2v13','M9 8l12-2','M9 18c0 3-6 4-6 1s6-4 6-1','M21 16c0 3-6 4-6 1s6-4 6-1'],
  layers:['m12 3 9 5-9 5-9-5z','m3 12 9 5 9-5','m3 16 9 5 9-5'], jobs:['M8 6h13','M8 12h13','M8 18h13','M3 6h.01','M3 12h.01','M3 18h.01'],
  settings:['M4 6h16','M4 12h16','M4 18h16','M8 3v6','M16 9v6','M10 15v6'], system:['M3 4h18v12H3z','M8 21h8','M12 16v5','m7 8 2 2-2 2','M12 12h4'],
  palette:['M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-4 2 2 0 0 1 1-4h3a3 3 0 0 0 3-3 9 9 0 0 0-9-7z','M7 9h.01','M11 6h.01','M16 7h.01'],
  git:['M6 3v12a6 6 0 0 0 12 0V9','M6 7h.01','M18 5h.01'], plus:['M12 5v14','M5 12h14'], arrow:['M4 12h16','m14 6 6 6-6 6'],
  external:['M14 3h7v7','m21 3-11 11','M10 3H3v18h18v-7'], close:['m6 6 12 12','M6 18 18 6'], menu:['M4 6h16','M4 12h16','M4 18h16'],
  search:['M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14','m15 15 6 6'], check:['m5 12 4 4L19 6'], chevron:['m8 10 4 4 4-4'],
  refresh:['M20 7v5h-5','M4 17v-5h5','M6 6a8 8 0 0 1 13 1','M18 18a8 8 0 0 1-13-1'], play:['m8 5 11 7-11 7z'],
  shield:['m12 3 9 4v5c0 5-9 9-9 9s-9-4-9-9V7z','m8 12 3 3 5-6'], download:['M12 3v12','m7 10 5 5 5-5','M4 16v5h16v-5'],
  activity:['M2 12h4l3-9 6 18 3-9h4'], wallet:['M3 5h18v15H3z','M3 9h18','M15 14h3']
};
</script>
<template><svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path v-for="(path,index) in (paths[name] || paths.sparkles)" :key="index" :d="path"/></svg></template>
