// The data response is parsed as a string; remote JavaScript is never evaluated.
const decode = require('./vendor/sina-decoder.cjs');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  try {
    const result = decode(JSON.parse(input));
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    process.stderr.write(String(error));
    process.exitCode = 1;
  }
});
