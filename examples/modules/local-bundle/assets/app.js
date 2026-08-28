// Replace this with your build output, or symlink it:
//
//   ln -s ~/work/site/dist/app.js \
//         ~/.pporlock/modules/local-bundle/assets/app.js
//
// Symlinks are resolved before the containment check, so a link pointing out of
// assets/ is refused. Copy, or point the rule at a path inside assets/.
console.info('[pporlock] local-bundle is serving this file');
