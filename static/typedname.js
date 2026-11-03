// How a person is doing while they type a NAME to confirm something dangerous: the popup for deleting a backup (static/confirm.js)
// and the one for dropping a database (static/review.js) both show this line under the box.
//
//   pgdasTypedName.describe(typed, word, buttonLabel)  ->  {text, state}
//
// `state` is idle (nothing typed), partial (the start of the name), wrong, or ok (exactly the name, case included).
// The text never spells the name out: the person has to type it themselves.
(function () {
  function describe(typed, word, button) {
    typed = typed || ""; word = word || "";
    if (!typed) return {state: "idle", text: "Type the name to enable “" + button + "”."};
    if (typed === word) return {state: "ok", text: "Matches. Press “" + button + "” to continue."};
    if (word.indexOf(typed) === 0) return {state: "partial", text: "Keep typing… (" + typed.length + " of " + word.length + " characters)"};
    if (typed.indexOf(word) === 0) return {state: "wrong", text: "Remove the extra characters after the name."};
    if (typed.toLowerCase() === word.toLowerCase()) return {state: "wrong", text: "Capital letters matter: the name must match exactly."};
    return {state: "wrong", text: "That does not match. Check the spelling."};
  }
  window.pgdasTypedName = {describe: describe};
})();
