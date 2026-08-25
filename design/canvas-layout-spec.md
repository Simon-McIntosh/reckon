# Canvas layout specification

Extracted mechanically from `design/reckon-spa-prototype.dc.html`. Every value below is
authored in the canvas, not paraphrased. Where an implementation and this file disagree,
this file is the specification.

## Root shell

```
<div> display:flex;flex-direction:column;height:100vh;overflow-y:hidden;overflow-x:auto;min-width:1374px
```


## Topbar and Manage

```
<div> display:flex;gap:18px;padding:9px 18px;border-bottom:1px solid var(--line);flex:none
  <div> display:flex;gap:9px
    <span> width:20px;height:20px;display:grid;font-family:var(--mono);font-size:11px
    <span> font-weight:600;font-size:14px;letter-spacing:-0.01em
  <div> display:flex;gap:4px;border-left:1px solid var(--line)
    <button> font-size:12px;padding:4px 9px
  <button> display:inline-flex;gap:8px;font-size:13px;padding:4px 10px
    <span> font-family:var(--mono);font-size:10.5px;padding:1px 5px
  <div> display:flex;gap:3px
<div> padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:14px
  <span> flex:none;font-size:11.5px;font-weight:600;letter-spacing:.07em
  <div> flex:1;min-width:0;display:flex;gap:5px
  <span> flex:none;font-family:var(--mono);font-size:11px
```

## Plans — four columns

```
<div> flex:1;display:flex;min-height:0;min-width:1374px
  <div> width:64px;flex:none;border-right:1px solid var(--line);padding:10px 8px;display:flex;gap:14px;overflow:auto
    <div> display:flex;gap:4px
    <div> height:1px
    <div> display:flex;gap:3px
  <div> width:390px;flex:none;border-right:1px solid var(--line);display:flex;min-height:0
    <div> display:flex;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line);flex:none
      <span> font-family:var(--mono);font-size:12px
      <div> display:inline-flex;overflow:hidden
      <button> width:25px;height:25px;font-size:11px
      <button> width:25px;height:25px;font-size:12px
    <div> flex:1;overflow:auto
        <div> display:flex;gap:8px
          <div> min-width:0;flex:1
          <span> font-family:var(--mono);font-size:10px;padding:2px 5px;flex:none
        <div> display:flex;gap:10px;font-family:var(--mono);font-size:11.5px
  <div> flex:1;min-width:0;display:flex
    <div> flex:1;min-width:300px;overflow:auto;padding:22px 26px 34px
        <div> display:flex;gap:10px;font-family:var(--mono);font-size:11.5px
          <button> font-size:11.5px;padding:2px 8px;flex:none
          <button> font-size:11.5px;padding:2px 8px;flex:none
        <h2> font-weight:600;font-size:23px;letter-spacing:-0.015em
        <div> display:flex;gap:11px;font-family:var(--mono);font-size:11.5px
          <button> font-size:11.5px;padding:2px 8px;flex:none
          <span> padding:1px 6px
        <h2> font-weight:600;font-size:23px;letter-spacing:-0.015em
        <div> overflow:hidden
          <div> display:flex;gap:8px;padding:7px 13px;border-bottom:1px solid var(--line)
          <div> font-weight:600;font-size:15px
        <div> border-top:1px solid var(--line)
          <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <div> display:flex;gap:6px
        <div> border-top:1px solid var(--line)
          <div> display:flex;gap:9px
        <div> border-top:1px solid var(--line)
          <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <div> display:flex;gap:9px
        <div> border-top:1px solid var(--line)
          <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <div> display:flex;gap:9px
    <div> width:300px;flex:none;border-left:1px solid var(--line);padding:16px 14px;display:flex;gap:12px;overflow:auto
      <div> display:flex;gap:6px
        <div> display:flex;gap:7px
          <span> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <span> flex:1;height:1px
          <span> font-family:var(--mono);font-size:11px
          <div> display:flex;gap:7px
          <div> font-weight:600;font-size:13px
          <div> font-size:12.5px
          <div> display:flex;gap:8px;font-family:var(--mono);font-size:11px
      <div> border-top:1px solid var(--line);display:flex;gap:5px;font-family:var(--mono);font-size:11.5px
        <button> display:flex;gap:8px;font-size:11.5px;padding:4px 7px
```

## Overview

```
<div> flex:1;overflow:auto;padding:20px 26px 40px
  <div> display:flex;gap:0;overflow:hidden
    <div> padding:11px 16px;min-width:130px;border-right:1px solid var(--line)
      <div> font-family:var(--mono);font-size:10px;letter-spacing:.07em
      <div> display:flex;gap:7px
        <span> font-family:var(--mono);font-size:20px;font-weight:500
        <span> font-size:11.5px
    <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
    <div> display:flex;gap:8px
        <div> display:flex;gap:9px
          <span> font-family:var(--mono);font-size:11px;padding:1px 6px
          <span> font-family:var(--mono);font-size:11px;padding:1px 6px
          <span> font-family:var(--mono);font-size:11px
          <span> font-family:var(--mono);font-size:11px
        <div> font-size:13.5px;max-width:110ch
        <div> padding:6px 9px;font-family:var(--mono);font-size:11.5px
    <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
    <div> overflow:hidden
      <div> display:grid;grid-template-columns:130px minmax(0,1fr) 70px 70px 60px 60px;gap:12px;padding:8px 12px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:10px;letter-spacing:.06em
        <span> font-family:var(--mono);font-size:12.5px;font-weight:500
        <span> font-size:13px;overflow:hidden
        <span> font-family:var(--mono);font-size:12px
        <span> font-family:var(--mono);font-size:12px
```

## Command palette

```
<div> position:fixed;display:flex
  <div> width:560px;max-width:92vw;overflow:hidden
    <div> display:flex;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line)
      <span> font-family:var(--mono);font-size:12px
      <input> flex:1;font-size:15px
      <span> font-family:var(--mono);font-size:10.5px;padding:1px 6px
      <span> font-size:13.5px;overflow:hidden;flex:1
      <span> font-family:var(--mono);font-size:11px;flex:none
      <span> font-family:var(--mono);font-size:11px;flex:none
```

## Reading mode

```
<div> position:fixed;display:flex
  <div> flex:none;display:flex;gap:12px;padding:10px 20px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11.5px
    <button> font-size:11.5px;padding:3px 9px;flex:none
    <button> font-size:11.5px;padding:3px 9px;flex:none
    <span> display:flex;gap:9px
      <span> padding:1px 6px
  <div> flex:1;overflow:auto
    <div> max-width:720px;padding:56px 24px 120px
        <div> display:flex;gap:9px
          <span> font-family:var(--mono);font-size:11.5px
          <span> font-family:var(--mono);font-size:11.5px
        <h2> font-weight:600;font-size:30px;letter-spacing:-0.02em
        <div> font-family:var(--mono);font-size:11.5px
        <h2> font-weight:600;font-size:32px;letter-spacing:-0.022em
        <div> display:flex;gap:9px;padding:8px 0 20px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11.5px
          <span> width:6px;height:6px
          <button> font-size:11px;padding:2px 8px
          <div> font-weight:600;font-size:18px
        <div> border-top:1px solid var(--line)
          <div> display:flex;gap:9px
          <div> display:flex;gap:8px
```

## Sprints — overview and board

```
<div> flex:1;overflow:auto;padding:20px 26px 40px
  <div> display:flex;gap:12px
    <span> font-weight:600;font-size:17px;letter-spacing:-0.012em
    <span> font-family:var(--mono);font-size:11.5px
    <div> display:inline-flex;overflow:hidden
    <div> display:flex;gap:10px;padding:8px 11px
      <span> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
      <div> display:inline-flex;overflow:hidden
    <div> display:grid;grid-template-columns:210px minmax(0,1fr);gap:0 16px;font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;border-bottom:1px solid var(--line)
      <div> display:grid;grid-template-columns:repeat(6,1fr)
    <div> display:grid;grid-template-columns:210px minmax(0,1fr);gap:0 16px;padding:8px 0;border-bottom:1px solid var(--line)
      <div> display:flex;gap:8px;min-width:0
        <span> font-family:var(--mono);font-size:12px
        <span> font-size:12.5px
        <span> font-family:var(--mono);font-size:11px
      <div> position:relative;height:15px
        <div> position:absolute;width:100%;height:2px
      <div> display:flex;gap:8px;min-width:0
        <span> font-family:var(--mono);font-size:12px
        <span> font-size:13px;overflow:hidden
      <div> position:relative;height:21px
    <div> display:flex;gap:16px;font-family:var(--mono);font-size:11px
      <span> display:inline-flex;gap:6px
        <span> width:14px;height:8px
      <span> display:inline-flex;gap:6px
        <span> width:14px;height:8px
      <span> display:inline-flex;gap:6px
        <span> width:14px;height:8px
    <div> display:flex;gap:10px
      <button> width:26px;height:26px
      <button> width:26px;height:26px
        <span> width:6px;height:6px
        <span> font-family:var(--mono);font-size:11.5px;padding:0 5px
    <div> overflow:hidden
      <div> display:flex;gap:10px;padding:8px 13px;border-bottom:1px solid oklch(0.88 0.08 75)
        <span> font-size:12px;font-weight:600;letter-spacing:.04em
        <span> font-size:12.5px
      <div> display:grid;grid-template-columns:190px minmax(0,1fr) 130px 200px;gap:14px;padding:9px 13px;border-bottom:1px solid var(--line)
        <div> min-width:0
          <div> font-size:12.5px;font-weight:500;overflow:hidden
          <div> font-family:var(--mono);font-size:11px
        <div> font-size:13px;overflow:hidden
        <div> font-family:var(--mono);font-size:11px
        <div> display:flex;gap:6px
          <button> font-size:12px;font-weight:500;padding:4px 10px
          <button> font-size:12px;font-weight:500;padding:4px 10px
    <div> display:grid;grid-template-columns:repeat(3,1fr);gap:14px
      <div> padding:11px
        <div> display:flex;gap:8px
          <span> font-size:11.5px;font-weight:600;letter-spacing:.07em
          <span> font-family:var(--mono);font-size:11.5px
        <div> display:flex;gap:8px
```

## Crew

```
<div> flex:1;overflow:auto;padding:20px 26px 40px
  <div> display:flex;gap:12px
    <span> font-weight:600;font-size:17px;letter-spacing:-0.012em
    <span> font-family:var(--mono);font-size:11.5px
    <span> font-family:var(--mono);font-size:11.5px
  <div> display:flex;gap:10px
      <div> display:grid;grid-template-columns:minmax(0,1fr) 150px 200px;gap:18px
        <div> min-width:0
          <div> display:flex;gap:8px;font-family:var(--mono);font-size:11.5px;overflow:hidden
          <div> display:flex;gap:8px
          <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <div> display:flex;font-family:var(--mono);font-size:12px
          <div> height:3px
          <div> font-family:var(--mono);font-size:11px
          <div> font-family:var(--mono);font-size:10.5px;letter-spacing:.06em
          <div> display:flex;gap:4px
        <div> display:flex;gap:10px;border-top:1px solid var(--line)
          <span> font-family:var(--mono);font-size:11px
          <span> display:inline-flex;gap:6px
        <div> padding:7px 10px;font-family:var(--mono);font-size:11.5px;display:flex;gap:9px
```

## Graph

```
<div> flex:1;overflow:auto;padding:20px 26px 40px
  <div> display:flex;gap:10px;border-bottom:1px solid var(--line)
    <span> font-family:var(--mono);font-size:12px;padding:2px 8px
    <span> font-weight:600;font-size:16px;letter-spacing:-0.01em
    <span> font-family:var(--mono);font-size:11.5px
  <div> display:flex;gap:9px;padding:9px 0;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px
    <span> font-size:10.5px;letter-spacing:.06em
  <div> display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px
    <div> display:flex;gap:5px
        <span> font-family:var(--mono);font-size:11.5px
        <span> font-size:13px
        <span> font-family:var(--mono);font-size:11px
      <button> font-size:12.5px;padding:7px 9px
    <div> padding:16px
      <div> display:flex;gap:0;overflow:hidden
          <div> font-family:var(--mono);font-size:10px;letter-spacing:.07em
          <div> display:flex;gap:6px
        <div> padding:0 12px
          <div> display:inline-flex;overflow:hidden
      <div> padding:12px 2px 2px
          <svg> position:absolute;overflow:visible
      <div> overflow:hidden
        <div> display:flex;gap:10px;padding:8px 13px;border-bottom:1px solid oklch(0.88 0.08 75)
          <span> font-size:12px;font-weight:600;letter-spacing:.04em
          <span> font-size:12.5px
        <div> display:grid;grid-template-columns:190px minmax(0,1fr) 120px 200px;gap:14px;padding:9px 13px;border-bottom:1px solid var(--line)
          <div> min-width:0
          <div> font-size:13px;overflow:hidden
          <div> font-family:var(--mono);font-size:11px
          <div> display:flex;gap:6px
      <div> display:flex;gap:16px;border-top:1px solid var(--line);font-family:var(--mono);font-size:11px
        <span> display:inline-flex;gap:5px
          <span> width:16px;height:1px
        <span> display:inline-flex;gap:5px
          <span> width:16px;height:1px
        <span> display:inline-flex;gap:5px
          <span> width:16px;height:1px
        <span> display:inline-flex;gap:5px
          <span> padding:1px 5px;font-size:9.5px
```
