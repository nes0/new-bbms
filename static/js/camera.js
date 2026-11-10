/*
 * "Take a photo" beside a file box, as well as choosing a file (profile photo 06 §6.1, proof of identity 03 §3.1).
 *
 * Mark the box: <input type="file" data-camera="user">         the front camera (a selfie)
 *               <input type="file" data-camera="environment">  the rear camera (a document)
 *               add data-camera-square to show the square the photo is cropped to.
 *
 * - Phones and tablets: the button opens the device's own camera app (full quality, autofocus).
 * - Computers: for a selfie, the webcam in a window here (snap, retake, use); for a document, a tip to use a phone,
 *   as webcams take poor photos of documents.
 * Either way the picture goes into the file box as if it had been chosen, so the form and the server's checks are
 * unchanged. Without JavaScript or a camera, the file box works as before. Also used for expense receipts and for
 * equipment photos in the admin.
 */
(() => {
  const touch = window.matchMedia("(pointer: coarse)").matches;
  const webcam = window.isSecureContext && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);

  function put(input, file) {
    const files = new DataTransfer();
    files.items.add(file);
    input.files = files.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function button(text) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "camera-button";
    b.textContent = text;
    return b;
  }

  // A small preview of what's in the box, so it's clear a photo was taken.
  function preview(input, holder) {
    input.addEventListener("change", () => {
      holder.replaceChildren();
      const file = input.files && input.files[0];
      if (!file || !file.type.startsWith("image/")) return;
      const img = document.createElement("img");
      img.className = "camera-preview";
      img.alt = "The photo you've chosen";
      img.src = URL.createObjectURL(file);
      img.onload = () => URL.revokeObjectURL(img.src);
      holder.append(img);
    });
  }

  // Phones: a hidden file box with `capture` opens the camera app; what it takes is moved into the real box.
  function nativeCamera(input, facing) {
    const hidden = document.createElement("input");
    hidden.type = "file";
    // Only image types, or some phones offer a file picker instead of the camera (the ID box also takes PDFs).
    const images = (input.accept || "").split(",").filter((type) => type.trim().startsWith("image/"));
    hidden.accept = images.length && !images.includes("image/*") ? images.join(",") : "image/*";
    hidden.setAttribute("capture", facing);
    hidden.hidden = true;
    hidden.addEventListener("change", () => {
      if (hidden.files[0]) put(input, hidden.files[0]);
      hidden.value = "";
    });
    return hidden;
  }

  // Computers: the webcam in a dialog.
  function webcamDialog(input, facing, square) {
    const dialog = document.createElement("dialog");
    dialog.className = "camera";
    dialog.innerHTML = `
      <div class="camera-frame${square ? " square" : ""}">
        <video autoplay playsinline muted></video>
        <canvas hidden></canvas>
      </div>
      <p class="camera-message hint" role="status"></p>
      <div class="actions">
        <button type="button" data-do="snap">Take the photo</button>
        <button type="button" data-do="retake" hidden>Try again</button>
        <button type="button" data-do="use" hidden>Use this photo</button>
        <button type="button" class="link" data-do="cancel">Cancel</button>
      </div>`;
    const video = dialog.querySelector("video");
    const canvas = dialog.querySelector("canvas");
    const message = dialog.querySelector(".camera-message");
    const buttons = Object.fromEntries([...dialog.querySelectorAll("[data-do]")].map((b) => [b.dataset.do, b]));
    let stream = null;

    function show(taken) {
      video.hidden = taken;
      canvas.hidden = !taken;
      buttons.snap.hidden = taken;
      buttons.retake.hidden = !taken;
      buttons.use.hidden = !taken;
    }

    function stop() {
      if (stream) stream.getTracks().forEach((track) => track.stop());
      stream = null;
      video.srcObject = null;
    }

    async function start() {
      show(false);
      buttons.snap.disabled = true;
      message.textContent = "Starting the camera…";
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: facing, width: { ideal: 1920 }, height: { ideal: 1080 } },
          audio: false,
        });
        video.srcObject = stream;
        video.classList.toggle("mirror", facing === "user");
        message.textContent = facing === "user" ? "Look at the camera, with your face in the middle." : "";
        buttons.snap.disabled = false;
      } catch (error) {
        message.textContent =
          error && error.name === "NotAllowedError"
            ? "The browser wasn't allowed to use the camera. You can allow it in the browser's settings, or choose a file instead."
            : "No camera could be started. Please choose a file instead.";
      }
    }

    buttons.snap.addEventListener("click", () => {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext("2d").drawImage(video, 0, 0);
      show(true);
      message.textContent = "Happy with it?";
    });
    buttons.retake.addEventListener("click", () => {
      show(false);
      message.textContent = "";
    });
    buttons.use.addEventListener("click", () => {
      canvas.toBlob(
        (blob) => {
          if (blob) put(input, new File([blob], "camera.jpg", { type: "image/jpeg" }));
          dialog.close();
        },
        "image/jpeg",
        0.92,
      );
    });
    buttons.cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", stop);

    return {
      dialog,
      open() {
        dialog.showModal();
        start();
      },
    };
  }

  function enhance(input) {
    if (input.dataset.cameraReady || input.name.includes("__prefix__")) return; // the admin's blank row template
    input.dataset.cameraReady = "1";
    const facing = input.dataset.camera === "environment" ? "environment" : "user";
    const row = document.createElement("span");
    row.className = "camera-row";
    const holder = document.createElement("span");
    input.after(row, holder);
    preview(input, holder);

    if (touch) {
      const hidden = nativeCamera(input, facing);
      const take = button(facing === "user" ? "Take a selfie" : "Take a photo of it");
      take.addEventListener("click", () => hidden.click());
      row.append(take, hidden);
    } else if (webcam && facing === "user") {
      const camera = webcamDialog(input, facing, "cameraSquare" in input.dataset);
      const take = button("Use your webcam");
      take.addEventListener("click", () => camera.open());
      row.append(take, camera.dialog);
    } else if (facing === "environment") {
      const tip = document.createElement("span");
      tip.className = "hint";
      tip.textContent = "Tip: open this page on a phone to photograph it with the phone's camera.";
      row.append(tip);
    }
  }

  const all = (root) => root.querySelectorAll("input[type=file][data-camera]").forEach(enhance);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => all(document));
  else all(document); // the admin loads this in <head>; the site pages with `defer`
  // The admin's "Add another" rows (equipment photos).
  document.addEventListener("formset:added", (event) => all(event.target));
})();
