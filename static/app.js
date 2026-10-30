// Layout helpers: mobile menu toggle.
(function () {
  var toggle = document.getElementById("toggle");
  if (toggle) toggle.addEventListener("click", function () { document.body.classList.toggle("menu-open"); });
})();
