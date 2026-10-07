document.querySelectorAll(".image-viewer-stage").forEach((stage) => {
  const image = stage.querySelector("img");
  let drag = null;

  function updatePanState() {
    stage.classList.toggle("can-pan",
      stage.scrollWidth > stage.clientWidth || stage.scrollHeight > stage.clientHeight);
  }

  function endDrag(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const pointerId = drag.pointerId;
    drag = null;
    stage.classList.remove("is-panning");
    if (stage.hasPointerCapture(pointerId)) stage.releasePointerCapture(pointerId);
  }

  image.draggable = false;
  stage.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "mouse" || event.button !== 0 || event.target !== image) return;
    updatePanState();
    if (!stage.classList.contains("can-pan")) return;
    event.preventDefault();
    drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      left: stage.scrollLeft,
      top: stage.scrollTop,
    };
    stage.setPointerCapture(event.pointerId);
    stage.classList.add("is-panning");
  });
  stage.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    stage.scrollLeft = drag.left + drag.x - event.clientX;
    stage.scrollTop = drag.top + drag.y - event.clientY;
  });
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
  stage.addEventListener("lostpointercapture", endDrag);
  const observer = new ResizeObserver(updatePanState);
  observer.observe(stage);
  observer.observe(image);
});
