// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors

#include std_head_vs.inc

varying vec2 texcoordoutf;
varying vec2 texcoordoutb;

void main(void) {
  // Front texture UV: scaled and offset by unif[14], unif[16]
  // Back  texture UV: scaled and offset by unif[15], unif[17]
  texcoordoutf = texcoord * unif[14].xy - unif[16].xy;
  texcoordoutb = texcoord * unif[15].xy - unif[17].xy;
  gl_Position = modelviewmatrix[1] * vec4(vertex, 1.0);
  dist = gl_Position.z;
  gl_PointSize = unib[2][2] / dist;
}
