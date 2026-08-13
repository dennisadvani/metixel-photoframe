// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#include std_head_fs.inc

varying vec2 texcoordoutf;
varying vec2 texcoordoutb;

void main(void) {
  vec4 texf = texture2D(tex0, texcoordoutf);
  vec4 texb = texture2D(tex1, texcoordoutb);

  // Clamp UVs: pixels outside [0,1] get opaque black (not transparent).
  // Using opaque black (alpha=1) ensures consistent crossfade blending
  // in letterbox/pillarbox areas and around images with transparency.
  // If we used transparent black (alpha=0), the mix() would produce
  // intermediate alpha values that cause visible square edges around
  // images with different aspect ratios.
  if (texcoordoutf.x < 0.0 || texcoordoutf.x > 1.0 ||
      texcoordoutf.y < 0.0 || texcoordoutf.y > 1.0) {
    texf = vec4(0.0, 0.0, 0.0, 1.0);
  }
  if (texcoordoutb.x < 0.0 || texcoordoutb.x > 1.0 ||
      texcoordoutb.y < 0.0 || texcoordoutb.y > 1.0) {
    texb = vec4(0.0, 0.0, 0.0, 1.0);
  }

  // Blend factor: unif[14][2] in GLSL = unif[44] in Python flat array
  float blend = unif[14][2];

  gl_FragColor = mix(texf, texb, blend);
}
