import { createHash } from "node:crypto"

/** Closed operation inputs; the selected profile validates its own requirements. */
export function mediaJob(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return
  const v = input as Record<string, unknown>, action = v.action
  const bounded = (value:unknown, maximum:number): value is string => typeof value === "string" && value.length > 0 && Buffer.byteLength(value,"utf8") <= maximum
  const operation = (value:unknown) => value === "image.generate" || value === "mesh.generate" || value === "mesh.rig" || value === "material.pack" || value === "material.bake" || value === "material.generate" || value === "image.render" || value === "audio.generate" || value === "image.pack" || value === "image.pack-crops"
  let args: Record<string, unknown>
  if (action === "profiles" && Object.keys(v).every((k) => k === "action" || k === "operation") && (v.operation === undefined || operation(v.operation)))
    args = v.operation === undefined ? { action } : { action, operation:v.operation }
  else if (action === "inspect" && Object.keys(v).sort().join(",") === "action,inputs,operation,parameters" && v.operation === "image.render" && Array.isArray(v.inputs) && v.inputs.length === 1 && v.inputs[0] && typeof v.inputs[0] === "object" && !Array.isArray(v.inputs[0]) && Object.keys(v.inputs[0]).sort().join(",") === "kind,resource_ref" && v.inputs[0].kind === "mesh" && bounded(v.inputs[0].resource_ref,8192) && v.parameters && typeof v.parameters === "object" && !Array.isArray(v.parameters) && Object.keys(v.parameters).join(",") === "variant" && (v.parameters as Record<string,unknown>).variant === "rig_clip") args = {action,operation:v.operation,inputs:[{kind:"mesh",resource_ref:v.inputs[0].resource_ref}],parameters:{variant:"rig_clip"}}
  else if ((action === "status" || action === "collect") && Object.keys(v).sort().join(",") === "action,job_id" && bounded(v.job_id,256)) args = { action, job_id:v.job_id }
  else if (action === "submit" && Object.keys(v).sort().join(",") === "action,destination,inputs,operation,parameters,profile_id") {
    if (!operation(v.operation) || !bounded(v.profile_id,256) || !Array.isArray(v.inputs) || v.inputs.length < 1 || v.inputs.length > 8 || !v.destination || typeof v.destination !== "object" || Array.isArray(v.destination) || !v.parameters || typeof v.parameters !== "object" || Array.isArray(v.parameters)) return
    const destination = v.destination as Record<string,unknown>
    if (Object.keys(destination).sort().join(",") !== "name,resource_ref" || !bounded(destination.resource_ref,8192) || !bounded(destination.name,255)) return
    const inputs: Record<string,unknown>[] = []
    for (const value of v.inputs) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return
      const item = value as Record<string,unknown>
      if (v.operation === "material.pack" && item.kind === "texture_map" && Object.keys(item).sort().join(",") === "kind,resource_ref,role" && ["baseColor","roughness","metallic","normal"].includes(item.role as string) && bounded(item.resource_ref,8192)) inputs.push({kind:item.kind,role:item.role,resource_ref:item.resource_ref})
      else if ((v.operation === "image.generate" || v.operation === "mesh.generate" || v.operation === "audio.generate" || v.operation === "material.generate") && item.kind === "workflow_recipe" && Object.keys(item).sort().join(",") === "kind,resource_ref" && bounded(item.resource_ref,8192)) inputs.push({kind:item.kind,resource_ref:item.resource_ref})
      else if ((v.operation === "material.bake" || v.operation === "image.render" || v.operation === "mesh.rig") && item.kind === "mesh" && v.inputs.length === 1 && Object.keys(item).sort().join(",") === "kind,resource_ref" && bounded(item.resource_ref,8192)) inputs.push({kind:item.kind,resource_ref:item.resource_ref})
      else if (item.kind === "image" && ((v.operation === "mesh.generate" && v.inputs.length === 1) || (v.operation === "image.pack" && v.inputs.length === 4) || (v.operation === "image.pack-crops" && v.inputs.length === 1)) && Object.keys(item).sort().join(",") === "kind,resource_ref" && bounded(item.resource_ref,8192)) inputs.push({kind:item.kind,resource_ref:item.resource_ref})
      else if ((v.operation === "image.generate" || v.operation === "mesh.generate") && item.kind === "text" && Object.keys(item).sort().join(",") === "kind,text" && bounded(item.text,16384)) inputs.push({kind:item.kind,text:item.text})
      else return
    }
    if (v.operation === "material.pack") {
      const roles = ["baseColor", "roughness", "metallic", "normal"]
      if (inputs.length < 3 || inputs.length > 4 || Object.keys(v.parameters).length !== 0 ||
          new Set(inputs.map((item) => item.role)).size !== inputs.length || new Set(inputs.map((item) => item.resource_ref)).size !== inputs.length ||
          !roles.slice(0,3).every((role) => inputs.some((item) => item.role === role))) return
      inputs.sort((a,b) => roles.indexOf(a.role as string) - roles.indexOf(b.role as string))
    }
    let rigParameters: { height_meters: number } | undefined
    if (v.operation === "mesh.rig") {
      const p=v.parameters as Record<string,unknown>, keys=Object.keys(p)
      if (inputs.length !== 1 || (keys.length !== 0 && (keys.length !== 1 || keys[0] !== "height_meters"))) return
      const height=p.height_meters === undefined ? 1.7 : p.height_meters
      if (typeof height !== "number" || !Number.isFinite(height) || height <= 0 || height > 10) return
      rigParameters={height_meters:height}
    }
    if (v.operation === "material.bake" && (inputs.length !== 1 || Object.keys(v.parameters).join(",") !== "resolution" || ![256,512,1024,2048].includes((v.parameters as Record<string,unknown>).resolution as number))) return
    if (v.operation === "image.render") {
      const p=v.parameters as Record<string,unknown>, keys=Object.keys(p).sort().join(",")
      if (inputs.length !== 1 || !(keys === "" || (keys === "variant" && p.variant === "four_heading") || (keys === "joint_ordinal,source_sha256,variant" && p.variant === "rig_clip" && typeof p.source_sha256 === "string" && /^[a-f0-9]{64}$/.test(p.source_sha256) && Number.isInteger(p.joint_ordinal) && (p.joint_ordinal as number) >= 0 && (p.joint_ordinal as number) <= 63))) return
    }
    if ((v.operation === "audio.generate" || v.operation === "material.generate") && (inputs.length !== 1 || inputs[0]?.kind !== "workflow_recipe")) return
    const sprite = v.operation === "image.pack" || v.operation === "image.pack-crops"
    let normalizedParameters: unknown = v.parameters
    if (sprite) {
      if (v.profile_id !== (v.operation === "image.pack" ? "sprite-sheet-v1" : "sprite-sheet-crops-v1")) return
      const p=v.parameters as Record<string,unknown>, crop=v.operation === "image.pack-crops"
      if (crop && Object.keys(p).sort().join(",") !== "annotations,rectangles") return
      const a=(crop ? p.annotations : p) as Record<string,unknown>
      if (!a || typeof a !== "object" || Array.isArray(a) || Object.keys(a).sort().join(",") !== "facing,fps,palette,pivots,style" || !Number.isInteger(a.fps) || (a.fps as number)<1 || (a.fps as number)>60) return
      const annotation=(x:unknown)=>x===null || (typeof x === "string" && x.isWellFormed() && !!x.trim() && Buffer.byteLength(x,"utf8")<=128 && !/[\u0000-\u001f\u007f]/u.test(x))
      if (![a.facing,a.palette,a.style].every(annotation) || !Array.isArray(a.pivots) || a.pivots.length!==4 || a.pivots.some((q:unknown)=>!q || typeof q!=="object" || Array.isArray(q) || Object.keys(q).sort().join(",")!=="x,y" || [ (q as any).x,(q as any).y ].some(n=>!Number.isInteger(n) || n<0 || n>512))) return
      const annotations={fps:a.fps,facing:a.facing,pivots:a.pivots.map((q:any)=>({x:q.x,y:q.y})),palette:a.palette,style:a.style}
      if (crop) {
        if (!Array.isArray(p.rectangles) || p.rectangles.length!==4 || p.rectangles.some((q:unknown)=>!q || typeof q!=="object" || Array.isArray(q) || Object.keys(q).sort().join(",")!=="height,width,x,y" || ["x","y","width","height"].some(k=>!Number.isInteger((q as any)[k]) || (q as any)[k] < (k==="x" || k==="y" ? 0 : 1) || (q as any)[k] > (k==="x" || k==="y" ? 4095 : 512)))) return
        normalizedParameters={annotations,rectangles:p.rectangles.map((q:any)=>({x:q.x,y:q.y,width:q.width,height:q.height}))}
      } else normalizedParameters=annotations
    }
    const entries = Object.entries(v.parameters as Record<string,unknown>).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0)
    if (!sprite && (entries.length > 32 || entries.some(([k,x]) => !bounded(k,128) || (typeof x !== "string" && typeof x !== "boolean" && typeof x !== "number") || (typeof x === "string" && Buffer.byteLength(x,"utf8") > 16384) || (typeof x === "number" && (!Number.isFinite(x) || Math.abs(x) > Number.MAX_SAFE_INTEGER))))) return
    args = {action, operation:v.operation, profile_id:v.profile_id, inputs, parameters:sprite ? normalizedParameters : rigParameters ?? Object.fromEntries(entries), destination:{resource_ref:destination.resource_ref,name:destination.name}}
  } else return
  const encoded = JSON.stringify(["openclank.media-job.v1",args])
  if (Buffer.byteLength(encoded,"utf8") > 262144) return
  return {argsJSON:encoded,argsSHA256:createHash("sha256").update(encoded,"utf8").digest("hex")}
}
